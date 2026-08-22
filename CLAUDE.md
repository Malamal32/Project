# Working in this repo

Read `README.md` first — it is the current, accurate description of the system.
`PROMPT.md` is the original spec and is superseded in two marked places (the stack
moved off Postgres to Cloudflare D1; `service/` and `frontend/` were not anticipated
in its repo layout). When they disagree, `README.md` wins.

## The shape of it

Three components, one database between them:

- **`pipeline/` + `models/`** — the supply side. One-shot CLI stages
  (`python -m pipeline.<stage>`), all idempotent. Loads reference data, ingests
  postings, publishes to D1.
- **`service/`** — the demand side. A long-running FastAPI app: reads a student's
  transcript, stores what they approve, drafts a resume against hiring demand.
- **`frontend/`** — Pathfinder, the browser client for `service/`. Alpine.js, plain
  ES modules, no build step, no `package.json`.

`service/` and `frontend/` never import from `pipeline/`, and the pipeline runs with
none of their dependencies loaded. Keep it that way.

## Invariants

These are the things that make the product worth anything. Breaking one is not a
bug you can fix later — it produces confident, fluent, wrong output.

**Nothing is asserted without evidence.** A posting requirement carries a verbatim
excerpt and character offsets into the raw document. A resume claim carries ids into
the student's profile, and `service/resume_evidence.py` deletes any claim whose ids
don't resolve or don't support the text. Both drop rather than guess, and both report
what they dropped and why.

The one stage that writes *upstream* of that contract is `service/description_polish.py`,
which replaces a description the evidence text is later built from. It carries its own
guards in `service/text_guards.py` — including a check the evidence contract cannot
express, that the rewrite dropped no skill keyword. If you add another stage that
rewrites student-supplied text in place, it needs the same two.

**Never a template fallback for generated prose.** Extraction falls back to regex
when the model is unavailable — a worse reading of a document that still exists.
Resume generation and description polish have *no* fallback, deliberately:
string-template prose is what both stages exist to replace. An outage returns
`success=False` and says so. Do not add one, in Python or in JS.

**Precision over recall, and record the reason.** US-scope, PII redaction, and
company dedupe all exclude rather than guess, and all store *why*
(`postings.us_scope_reason`, `extraction_method`). Ambiguous company merges go to
review, not to auto-merge.

**Raw payloads are immutable.** Content-addressed keys in R2 (`pipeline/raw_store.py`).
Parsing is always re-runnable from raw.

## Two places that must move together

**The evidence-id scheme.** `service/resume_evidence.py` mints ids by *position* in
the profile's `skills` / `certifications` / `honors` lists. The browser builds that
profile from two pieces of wizard state, so its concatenation order is the scheme.
Defined once in `collectProfileItems` (`frontend/js/services/api-service.js`);
`tests/test_evidence_id_parity.py` runs the real browser code under node and fails if
the two drift. If they drift, every skill claim is dropped silently.

**The LinkedIn whitelist.** `WANTED_FILES` in
`frontend/js/services/linkedin-import.js` decides which archive members leave the
student's machine; `EXPORT_FILES` in `service/linkedin_import.py` decides which
get parsed on arrival. Duplicated on purpose — one filename list standing between
a LinkedIn archive's messages and an HTTP request is a single point of failure —
but only useful while they agree. `tests/test_linkedin_parity.py` fails if they
drift. Never add a file to one without the other, and never add one at all
without asking whose data is in it.

**The matcher mirrors.** `frontend/js/services/api-service.js` mirrors
`service/market_matching.py` (display only — the server always recomputes), and
`frontend/js/services/academic-extraction.js` mirrors
`service/academic_extraction.py` (the offline fallback path). Change both or neither.

## Schema changes

`migrations/d1/*.sql` is the single source of truth and is **generated**, not
hand-written:

```sh
uv run python -m scripts.generate_d1_schema   # edit models/ first, then regenerate
uv run python -m pipeline.init_db             # apply locally
```

Commit the models and the SQL together. `tests/test_schema_parity.py` fails if they
drift. Tests build their database by applying those same `.sql` files, not
`Base.metadata.create_all()`, so a model change that never reached a migration fails
in the suite rather than at deploy.

## Deployment

The product runs as a single Python Worker at
`coursefolio.stellic-pathfinders.workers.dev`:

```sh
uv run python -m scripts.build_worker
cd worker && uv run pywrangler deploy
```

`worker/entry.py` is the only deployment-specific file, and it must stay that
way: it swaps the profile store to D1 and binds the role-search reader, and
re-implements no endpoint. Anything else that differs between local and
deployed is a bug there, not a feature.

Two constraints shape it. **No native dependencies** — MarkItDown and pypdf are
imported lazily so the modules that need them stay importable on Pyodide, and
PDF text extraction moves to the browser. **No sync HTTP** — the Anthropic
clients are `AsyncAnthropic`, which is also why the service layer is async all
the way down.

## Before you say you're done

```sh
uv run pytest
```

Green means 330 passed, 1 xfailed. The suite needs no network and no database
service. Two tests shell out to `node` and skip without it
(`test_evidence_id_parity.py`, `test_linkedin_parity.py`) — if you changed
anything in `frontend/js/services/`, make sure they actually ran.

To see the whole product end to end:

```sh
SERVE_FRONTEND=true uv run uvicorn service.app:app --reload --port 8000
```

It works with no `ANTHROPIC_API_KEY`: extraction falls back to regex, and resume
generation and description polish report themselves unavailable. That is the correct
degraded behaviour, not a broken setup.

## House style

The prose in this codebase carries real weight — comments explain *why* a choice was
made and what breaks otherwise, not what the next line does. Match that. If you
change a decision, change the comment that justified it; a stale rationale is worse
than none, because the next reader will trust it.

Two mocked calls in `frontend/js/services/` stand in for endpoints Phases 4–6 will
provide (`analyzeMarket`, `lookupCourses`); `searchRoles` is real against D1 on the
deployed Worker and falls back to a short local list elsewhere. They are marked as
mocks in the source and listed in `README.md`. Keep that list true.
