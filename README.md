# hiring-db-pipeline

US-scoped data pipeline and database answering: **"For major X, which companies are
hiring, and what credentials, skills, and experience do those postings ask for?"**

Built in phases (see `PROMPT.md` for the full spec). Each phase stops for review
before the next begins.

## The three pieces, and two names

`hiring-db-pipeline` is the package and the infrastructure: the D1 database, the R2
bucket, the wrangler config, this repo. **Pathfinder** is the student-facing product
built on top of it — the name in the browser tab and in `PATHFINDER_ALLOWED_ORIGINS`.
Two names for two layers; they are not two projects.

| | What it is | Runs as |
|---|---|---|
| `pipeline/` + `models/` | The supply side. Collects postings, loads reference data, publishes to D1. | One-shot CLI stages |
| `service/` | The demand side. Reads a student's transcript, stores what they approve, drafts a resume against real demand. | A long-running HTTP service |
| `frontend/` | Pathfinder itself — the browser client for `service/`. | Static files, no build step |

`service/` and `frontend/` do not import from `pipeline/`, and the pipeline runs with
none of their dependencies loaded. What joins the two halves is the database.

## Stack

Python 3.11+ (`uv`), **Cloudflare D1** (SQLite) with raw payload bodies in
**Cloudflare R2**, SQLAlchemy 2.x, Pydantic v2, `httpx` + `tenacity`, `typer` CLI,
`structlog`, `pytest`. The transcript extraction service adds FastAPI and the
Anthropic SDK — see [Transcript extraction service](#transcript-extraction-service-service).

### Why there are two databases

The pipeline writes to a **local SQLite file** (`data/hiring_db.sqlite3`) and
`pipeline.sync_d1` publishes it to **Cloudflare D1**. This is not a staging copy — it
is the same engine on both ends. SQLite is the dialect D1 speaks, so the schema in
`migrations/d1/` is applied verbatim to both, and anything that works locally works
remotely.

The split exists because the pipeline needs real transactions: `ingest_postings`
snapshots a posting's previous head into `posting_versions` *before* mutating it, and
D1's HTTP API commits each statement independently, so that sequence could tear
partway through. D1's 100-bound-parameter limit also caps batched writes at a handful
of rows. Bulk-loading a finished database through `wrangler d1 execute --file` avoids
both problems and is Cloudflare's supported import path.

## Setup

### GitHub Codespaces / devcontainer

Open the repo in a Codespace (or "Reopen in Container" in VS Code) and everything
below is done for you: Python 3.12 + `uv`, a generated `.env`, `uv sync`, and
`python -m pipeline.init_db`. Config lives in `.devcontainer/`. No database service
runs — SQLite is a file.

### Local

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv isn't installed
uv sync

cp .env.example .env   # fill in PIPELINE_CONTACT_EMAIL and, for publishing, R2 keys

uv run python -m pipeline.init_db   # create data/hiring_db.sqlite3 + apply the schema
```

## Running a pipeline stage

Every stage is idempotent and runnable standalone:

```sh
uv run python -m pipeline.load_cip
```

## Tests

```sh
uv run pytest
```

Tests need no database service and no network. One test —
`tests/test_evidence_id_parity.py`, which checks the browser and the server agree on
evidence ids — shells out to `node` and skips when it isn't installed.

The `db_session` fixture in
`tests/conftest.py` gives each test its own throwaway SQLite file, built by applying
`migrations/d1/*.sql` — the exact DDL that ships to D1 — rather than
`Base.metadata.create_all()`. That way tests exercise the real constraints and the
FTS5 virtual table, and a model change that never reached a migration fails here
instead of at deploy time (`tests/test_schema_parity.py` enforces the link).

## Schema and migrations

`migrations/d1/*.sql` is the single source of truth, applied to both ends:

```sh
uv run python -m pipeline.init_db                      # local SQLite
wrangler d1 migrations apply hiring-db --remote        # Cloudflare D1
```

The DDL is generated from the ORM models, not hand-written:

```sh
uv run python -m scripts.generate_d1_schema
```

Edit `models/`, regenerate, and commit both. `tests/test_schema_parity.py` fails if
they drift.

### Types that changed leaving Postgres

D1/SQLite has no native UUID, JSONB, or timezone-aware timestamp. See `models/types.py`.

| Postgres | Now | Note |
|---|---|---|
| `UUID` | `sa.Uuid` → `CHAR(32)` | Still generated client-side by `uuid.uuid4` |
| `JSONB` | `sa.JSON` → TEXT | Only `sources.politeness_config`; read with `json_extract` |
| `TIMESTAMPTZ` | `TZDateTime` → `DATETIME` | Normalized to UTC. **Naive datetimes are rejected**, not assumed to be UTC |
| `pg_trgm` GIN index | FTS5 virtual table | See below |

`pgvector` is gone. It was provisioned for Phase 4 tier 3 (embedding similarity) but
never used by any code; that tier is dropped rather than ported.

### Fuzzy title search

`occupation_alt_titles_fts` is an FTS5 index over `occupation_alt_titles.alt_title`,
rebuilt by `pipeline.load_occupations`. It replaces the Postgres `pg_trgm` GIN index,
but it is **token-based full-text search, not trigram similarity** — it answers "which
occupations use this title" well, but it cannot produce a graded similarity ratio.
Phase 4's tier-2 confidence (spec'd as 0.70–0.85 scaled by similarity) will need its
own scheme rather than scaling a similarity score.

## Posting ingestion (Phase 3)

```sh
uv run python -m pipeline.load_sources        # seed/refresh the sources table
uv run python -m pipeline.ingest_postings      # full crawl of all enabled ats_api sources
uv run python -m pipeline.ingest_postings --source-name "Greenhouse: Stripe" --limit-per-source 20
```

Both stages are idempotent and safe to re-run: `load_sources` upserts on `name`,
`ingest_postings` upserts on `(source_id, source_posting_id)` and only writes a new
`posting_versions` row when the content actually changed — re-running against
unchanged upstream data just bumps `last_seen_at`.

Every fetch goes through the shared politeness layer (`pipeline/politeness.py`):
a `robots.txt` check that hard-fails the fetch if disallowed, a per-domain rate
limiter, retry-with-backoff on 5xx/429/transport errors, and an identifying
User-Agent built from `PIPELINE_CONTACT_EMAIL`. Every posting description is run
through `pipeline/pii_redaction.py` before it's stored, and US-scope is decided
by `pipeline/us_scope.py` — both intentionally favor precision over recall
(if it's not confidently in scope, it's excluded, not guessed), and both record
*why* a decision was made (`us_scope_reason` on `postings`) rather than hiding it.

### Raw payloads

`raw_documents` holds metadata and a **key**, not the body. Bodies go to R2 via
`pipeline/raw_store.py`, because D1 caps a row at 2 MB and a database at 10 GB and
raw descriptions are the one table here that grows without bound. Keys are
content-addressed (`raw/{sha256[:2]}/{sha256}`), so identical bytes always land in
the same place and nothing can overwrite anything — the "raw documents are immutable"
contract holds by construction. Read a body back with `raw_store.get(key)`.

**Without R2 credentials the store falls back to `data/raw_store/` on disk**, so every
stage and the whole test suite run offline. The keys are identical either way, so a
local store can be uploaded to R2 later without rewriting a row.

## Publishing to Cloudflare D1

```sh
uv run python -m pipeline.sync_d1 --dry-run   # build the SQL, touch nothing
uv run python -m pipeline.sync_d1             # build, load, and verify row counts
```

A full replace, not an incremental diff: drop, recreate from `migrations/d1/`, load,
then rebuild the FTS5 index on the far side (virtual tables can't be carried in a
dump). The stage reads row counts back out of D1 and fails if any table disagrees
with the local build.

The emitted SQL is shaped to D1's import rules — no `BEGIN`/`COMMIT`,
`PRAGMA defer_foreign_keys` up front, and INSERTs batched to stay under the 100 KB
statement limit.

## Transcript extraction service (`service/`)

The demand side of the same product: a student uploads an academic transcript and
gets back structured fields to review, which can then be matched against the
hiring data above.

```sh
uv run uvicorn service.app:app --reload --port 8000

# ...or with the browser client on the same origin (see Frontend below):
SERVE_FRONTEND=true uv run uvicorn service.app:app --reload --port 8000
```

| Endpoint | Does |
|---|---|
| `POST /api/transcript/parse` | PDF upload → validated → text → structured `AcademicProfile`. **Stores nothing.** |
| `POST /api/student/profile` | Stores a profile the student has reviewed. The only write path. |
| `POST /api/resume/generate` | Reviewed profile + market demand → a tailored resume. **Stores nothing.** |
| `GET /health` | Liveness. |

All three are live from the browser client — `frontend/` calls exactly these and
nothing else reaches the network.

This is a long-running service, not a pipeline stage, which is why it lives
outside `pipeline/`. No pipeline stage imports it, so the pipeline still runs
with no Anthropic key and none of the web dependencies loaded.

### Two extraction stages, one of them a fallback

`service/llm_extraction.py` is primary: the document text goes to the Claude API
under a cached system prompt, constrained to a schema by structured outputs. If
that stage is disabled, unreachable, rate-limited, refused, or returns something
unusable, `service/academic_extraction.py` — pure regex, no network — runs
instead. **An outage degrades result quality; it never fails the upload.** The
`extraction_method` field on every response (`"llm"` / `"rules"`) says which one
ran, and the startup log says which mode the deploy is in, so a missing key shows
up as an error line rather than as quietly worse output.

Turn the LLM stage off without a redeploy with `LLM_EXTRACTION_ENABLED=false`.

Neither stage ever invents a value. Anything not literally present in the document
comes back null with a student-facing warning, and `review_required` is always
`true` — the model's output is a draft for the student, never a fact.

### Data handling

- The uploaded PDF is written only to a randomly-named temp file for the duration
  of the conversion and deleted in a `finally` block. It never reaches
  `raw_documents` or R2 — that store is for job postings.
- The extracted document text is never logged and never persisted. It **is** sent
  to the Anthropic API for the extraction step; that is the whole point of the
  stage, and `LLM_EXTRACTION_ENABLED=false` is how you opt out of it.
- Nothing is logged from an upload but byte size, outcome, and token counts —
  never document text, never model output. API failures log a status code and
  type, never the request body.
- Uploads are treated as hostile: extension, declared content-type, magic bytes,
  size, encryption, and parseability are all checked before any converter runs,
  and the document text is delimited and framed as data in the prompt so a
  transcript cannot issue instructions to the model.
- Only `POST /api/student/profile` writes, and only the structured fields the
  student reviewed.

### Stored profiles

`student_profiles` plus its children `student_courses` (ordered by `position`,
preserving document order) and `student_attributes` (skills, honors,
certifications). Fields are stored **verbatim as extracted** — `"May 2027"` stays
`"May 2027"`, `"3.72/4.00"` keeps its scale — for the same reason
`postings.title_raw` keeps its source spelling. `extraction_method` and
`model_version` record which stage produced them.

`major` is deliberately free text: mapping it to a CIP code is the same problem
as mapping a posting's stated major, and belongs with the `major_alias` work in
Phase 6 rather than being solved twice.

## Resume generation (`service/resume_generation.py`)

`POST /api/resume/generate` takes a reviewed profile, the experience and projects
the student typed in, and a ranked list of skills employers actually request, and
returns a resume aimed at a target role.

The model's job is narrow, and the narrowness is the design: it decides what to
surface, what order to put it in, and how to word it. It has no authority over
what is true. A resume generator that can invent is worse than none at all,
because the fabrication is fluent, specific, and lands on the student in an
interview.

### The evidence contract

Every statement the model emits carries the ids of the profile items it rests on.
`service/resume_evidence.py` then resolves each id and deletes anything that does
not hold up — before the student sees it. This is the resume-side counterpart of
the guardrail PROMPT.md specifies for Phase 5 extraction; there the anchor is a
character offset into a posting, here it is an id into the profile.

A claim is dropped when it cites nothing, cites an id that does not exist, cites
the wrong item (a bullet under job A citing job B), states a figure absent from
its own evidence, names a skill the student never listed, reproduces a "verbatim"
field with different wording, or repeats another line. Drops are returned in
`dropped[]` with a reason rather than silently swallowed — "we left this out and
here is why" is a usable answer, and it keeps the guardrail visible.

The figure check earns its place. Given "helped optimize the checkout flow", a
model will readily produce "cut load time 40%" — plausible, well-phrased, and
invented. Every id resolves, because the bullet correctly cites the experience it
embellished. Only the number gives it away.

### Market demand is not evidence

`service/market_matching.py` scores each in-demand skill against the profile as
`verified`, `coursework`, `transferable`, or `not_verified`. Demand decides what
is worth checking and how to phrase it; it never counts as proof the student has
a skill. `not_verified` skills are reported as gaps and must not appear anywhere
in the resume — enforced twice, once structurally on the skills list and once
against free text, because the skills check alone cannot see a skill smuggled
into a summary sentence.

The match is always recomputed server-side. The browser mirrors the matcher in JS
and could send one, but it assigns its own evidence ids, and ids that do not
resolve here would drop every claim.

### Differences from the extraction stage

- **No fallback.** Falling back on extraction means a worse reading of a document
  that still exists; falling back here would mean generating prose from string
  templates, which is what this stage replaces. With the stage off or no key
  present, the endpoint returns `success=False` with a student-facing warning and
  still reports the gaps. `RESUME_GENERATION_ENABLED=false` is the kill switch.
- **Regeneration is a `variant` integer, not a temperature.** Sampling parameters
  are rejected on this model, so "regenerate wording" varies phrasing through the
  prompt while resting on identical evidence.

### Data handling

Same posture as extraction: persists nothing, logs no profile text and no model
output — only token counts, stop reasons, and drop reasons. The profile arrives
on the request, is used, and is gone when the response is sent.

Note the egress this adds. The transcript endpoint sends document text to the
Anthropic API; this one sends the student's reviewed academic record, work
history, and projects. That is the point of the stage, and the kill switch is how
you opt out — and the student is told, on the landing screen, at the upload step,
and again on the last screen before drafting.

## Frontend (`frontend/`)

Pathfinder: the ten-step wizard a student actually uses. Alpine.js, plain ES
modules, **no build step and no package.json** — the directory is served exactly as
it sits in the repo. `frontend/vendor/` holds Alpine and pdf.js checked in rather
than pulled from a CDN, so the app runs offline and a student's PDF never depends on
a third-party host.

```sh
SERVE_FRONTEND=true uv run uvicorn service.app:app --reload --port 8000
# open http://localhost:8000
```

That mounts `frontend/` at `/` on the same origin as the API, which means no CORS
and no second server. Serving it separately works too — set
`PATHFINDER_ALLOWED_ORIGINS` to its origin and `window.TRANSCRIPT_SERVICE_URL` to
the API's.

### What is live and what is still a mock

`frontend/js/services/api-service.js` is the only door to the network. Three calls
are real, against the endpoints above: transcript parse, profile save, resume
generate. Three are **mocks**, clearly marked as such, because the endpoint does not
exist yet:

| Mocked call | Would be | Blocked on |
|---|---|---|
| `searchRoles` | `GET /api/roles/search` | a query service over `occupations` / `occupation_alt_titles` |
| `analyzeMarket` | `POST /api/market/analyze` | Phases 4–6: postings must be classified and their requirements extracted before demand can be computed |
| `lookupCourses` | `GET /api/courses/lookup` | a course-catalog source, which this repo has no adapter for |

The pipeline builds the data the first two need; nothing yet queries it. Each mock
returns the shape its endpoint will return, so swapping one is a `fetch()` and
nothing else.

### The evidence-id scheme

`service/resume_evidence.py` mints evidence ids by **position** in the profile's
`skills` / `certifications` / `honors` lists. The browser builds that profile out of
two separate pieces of wizard state (`academic` and `activities`), so the order it
folds them in *is* the id scheme. It is defined once, in `collectProfileItems` in
`api-service.js`, and `tests/test_evidence_id_parity.py` runs the real browser code
under node and fails if the two sides stop agreeing. They must: if `skill_3` means
different things on each end, every skill claim in the resume is silently dropped as
`unknown_evidence_id`.

`tests/test_evidence_id_parity.py` skips when node is absent, so it does not run in
the Python-only devcontainer.

### Two mirrors of backend logic, and why

`academic-extraction.js` mirrors `service/academic_extraction.py` and runs only when
the extraction service is unreachable — the PDF is parsed in the tab instead, at the
quality the rule-based path always had. `matchMarket` in `api-service.js` mirrors
`service/market_matching.py` so the student's match renders without a round trip;
the server always recomputes it, and `POST /api/resume/generate` accepts no
client-supplied match. Both mirrors must move in step with the module they copy.

There is deliberately **no browser-side fallback for resume generation**. A template
summary is fluent, specific, unverifiable, and lands on the student in an interview
— so an outage says so instead.

## Adding a new source adapter

Implement the `Collector` protocol (`discover`, `fetch`, `parse`) as a sibling module
under `pipeline/collectors/`; see `pipeline/collectors/greenhouse.py` for the
reference implementation. A career-page collector must additionally call
`pipeline.allowlist.require_allowlisted(domain)` before fetching — populate
`data/reference/career_page_allowlist.csv` first (it ships empty by design).
A licensed-feed adapter needs real credentials in `.env` before it can be enabled;
see `data/reference/SOURCE.md` for the currently-stubbed Handshake source.

Collectors must produce **timezone-aware** datetimes — `TZDateTime` rejects naive
values rather than guessing a zone.

## Reference data provenance

Every file under `data/reference/` is documented in `data/reference/SOURCE.md`
(URL, retrieval date, SHA-256 hash).

## Credentials expected in `.env`

- `DATABASE_URL` — local SQLite path. Defaults to `data/hiring_db.sqlite3` if unset.
- `PIPELINE_CONTACT_EMAIL` — recommended before running Phase 3 collectors; used
  in the outbound User-Agent so a source operator can reach us if needed.
- `CLOUDFLARE_ACCOUNT_ID`, `D1_DATABASE_NAME` — required by `pipeline.sync_d1`.
  `wrangler` must be authenticated (`wrangler login`) for the same account.
- `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` — optional. Without them
  raw payloads go to `data/raw_store/` instead of R2.
- `ANTHROPIC_API_KEY` — optional, read by the SDK itself (nothing in this repo
  reads or stores it). Without it the transcript service falls back to rule-based
  extraction and resume generation returns `success=False`. Inject it from a
  secret manager in production.
- `LLM_EXTRACTION_ENABLED`, `ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT`,
  `ANTHROPIC_MAX_TOKENS`, `ANTHROPIC_TIMEOUT_SECONDS`, `ANTHROPIC_MAX_RETRIES`,
  `MAX_LLM_INPUT_CHARS` — all optional, all defaulted in `service/config.py`.
- `SERVE_FRONTEND` — optional, default false. True serves `frontend/` from the
  same app as the API.
- `PATHFINDER_ALLOWED_ORIGINS` — optional. Only needed when the frontend is served
  from a different origin than the API.
- `RESUME_GENERATION_ENABLED`, `RESUME_MODEL`, `RESUME_EFFORT`,
  `RESUME_MAX_TOKENS`, `RESUME_TIMEOUT_SECONDS`, `RESUME_MAX_RETRIES`,
  `RESUME_MAX_VARIANT` — the same dials for the resume stage, tunable separately
  because the two calls have different shapes. Also defaulted in
  `service/config.py`.
- Licensed feed credentials (e.g. Handshake) — added when a licensed-feed adapter
  begins.
