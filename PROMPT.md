# Claude Code Prompt — Major → Hiring Database (US)

> Paste everything below the line into Claude Code (or save it as `PROMPT.md` in an empty repo and tell Claude Code to follow it).

---

## Project

Build a US-scoped data pipeline and database that answers: **"For major X, which companies are hiring, and what credentials, skills, and experience do those postings ask for?"**

Work in phases. After each phase, stop and show me: the migration/DDL, the row counts loaded, and 5 sample rows. Do not proceed to the next phase until I confirm.

## Stack (use these unless you flag a concrete blocker)

> **Superseded after Phase 3.** The database moved from Docker PostgreSQL to
> **Cloudflare D1** (SQLite) with raw payload bodies in **R2**. Consequences for the
> phases below: Alembic is replaced by `migrations/d1/*.sql`; the `pg_trgm` fuzzy tier
> in Phase 4 is now FTS5 (token-based, so it yields no similarity ratio to scale
> confidence by); the `pgvector` embedding tier in Phase 4 is **dropped**; and the
> pytest fixture uses a throwaway SQLite file, not a Postgres schema. See `README.md`
> for the current stack and the reasoning.

- Python 3.11+, `uv` for dependency management
- PostgreSQL 16 (`pgvector` extension enabled for later similarity work)
- SQLAlchemy 2.x + Alembic for schema and migrations
- Pydantic v2 for all record validation at ingestion boundaries
- `httpx` for HTTP, `tenacity` for retries with exponential backoff
- CLI via `typer`; every pipeline stage must be runnable as `python -m pipeline.<stage>` and idempotent
- `pytest` with fixtures using a throwaway Postgres schema
- Structured logging (`structlog`) to stdout as JSON

Repo layout: `pipeline/` (stages), `models/` (ORM), `migrations/`, `data/reference/` (checked-in reference files), `tests/`, `scripts/`.

> **Also superseded.** Two directories not anticipated here now exist: `service/`
> (the FastAPI transcript-extraction and resume-generation service — the demand
> side) and `frontend/` (Pathfinder, its browser client). Neither is a pipeline
> stage and neither imports one. See `README.md`.

## Hard constraints — read before writing any collector

1. **No scraping of sites that disallow it.** The posting collector must only pull from (a) a licensed feed I supply credentials for, or (b) official ATS APIs (Greenhouse, Lever, Ashby, SmartRecruiters, Workable job-board endpoints), or (c) a company career page I have explicitly added to an allowlist file. Build the allowlist mechanism; leave it empty and let me populate it.
2. **Respect `robots.txt`, rate limits, and per-source ToS.** Implement a shared politeness layer: per-domain rate limiting, configurable crawl delay, identifying User-Agent, and a `robots.txt` check that hard-fails the fetch if disallowed.
3. **Store provenance for every derived fact.** Nothing enters the database as an assertion without a source record and, where the fact came from text, a verbatim evidence excerpt with character offsets.
4. **Never store PII from postings** (recruiter names, emails, phone numbers). Redact at ingestion with a tested regex + validation pass.
5. **Raw payloads are immutable.** Land raw JSON/HTML in a `raw_documents` table (or object storage with a DB pointer), hash it, and never mutate it. All parsing is re-runnable from raw.

---

## Phase 1 — Reference data: CIP 2020

Load the complete NCES CIP 2020 major file.

- Source: IPEDS / NCES CIP 2020 code list. Download it, check the file into `data/reference/` with a `SOURCE.md` recording the URL, retrieval date, and file hash.
- Table `cip_codes`: `cip_code` (canonical `XX.XXXX` string, PK), `cip_title`, `cip_definition`, `level` (2, 4, or 6 digit), `parent_cip_code` (self-FK), `is_active`, `crosswalk_notes`.
- Load **all three levels** and build the parent hierarchy. A 6-digit code rolls up to its 4-digit series, which rolls up to its 2-digit family. I need to query "Engineering" (2-digit) and "Mechanical Engineering" (6-digit) with the same join.
- Preserve CIP's "moved from / new in 2020" notes if present in the source — they matter for reconciling older data.
- Validate: no orphan parents, every 6-digit has a 4-digit ancestor, code format matches `^\d{2}\.\d{4}$` (or `^\d{2}$` / `^\d{2}\.\d{2}$` for higher levels).

## Phase 2 — Reference data: O*NET occupations + CIP–SOC crosswalk

- Load the O*NET occupation reference (O*NET-SOC taxonomy) into `occupations`: `onet_soc_code` (PK, `XX-XXXX.XX`), `title`, `description`, `soc_2018_code` (the 6-digit parent), `job_zone`, `bright_outlook` flag if available.
- Also load the O*NET **Alternate Titles** file into `occupation_alt_titles` — this is the single highest-leverage asset for Phase 4 classification. Index it for trigram search.
- Load the official **CIP-to-O*NET-SOC crosswalk** into `cip_soc_crosswalk`: `cip_code`, `onet_soc_code`, `crosswalk_source` (e.g. `nces_cip_soc_2020`, `onet_cip_soc`), `retrieved_at`. Composite PK on (cip_code, onet_soc_code, crosswalk_source).
- The crosswalk is **many-to-many in both directions** — do not collapse it. Write a test asserting at least one CIP maps to >1 SOC and at least one SOC maps to >1 CIP.
- Emit a coverage report: % of 6-digit CIP codes with ≥1 SOC mapping, % of O*NET-SOC codes with ≥1 CIP mapping. I expect gaps; I want them visible, not silently filled.

## Phase 3 — Posting ingestion

Build a source-agnostic collector framework, then one working adapter.

- `sources` table: `source_id`, `source_type` (`licensed_feed` | `ats_api` | `career_page`), `name`, `base_url`, `auth_mode`, `enabled`, `politeness_config` (JSONB), `terms_reviewed_at`.
- `companies` table: `company_id`, `canonical_name`, `normalized_name` (lowercased, suffix-stripped for matching), `domain`, `hq_state`, `hq_city`, `naics_code` (nullable), `size_bucket` (nullable), `first_seen_at`, `last_seen_at`. Include a simple deterministic dedupe on (normalized_name, domain) and log ambiguous merges to review rather than auto-merging.
- `postings` table: `posting_id` (UUID), `source_id`, `source_posting_id` (the source's own ID — unique per source, used for upsert), `company_id`, `title_raw`, `description_raw_ref` (FK to `raw_documents`), `employment_type`, `is_remote`, `location_city`, `location_state`, `location_country` (filter to US; keep a `us_scope_reason` field), `posted_at`, `first_seen_at`, `last_seen_at`, `closed_at`, `status` (`active` | `stale` | `closed`), `content_hash`.
- Write **one** reference adapter: the Greenhouse job-board API (`boards-api.greenhouse.io`), since it is public, documented, and ToS-clean for board data. Structure it so a licensed-feed adapter is a drop-in sibling implementing the same `Collector` protocol: `discover() -> Iterable[PostingStub]`, `fetch(stub) -> RawDocument`, `parse(raw) -> PostingRecord`.
- Upsert semantics: match on (`source_id`, `source_posting_id`). If `content_hash` changed, write a new row into `posting_versions` and update the head record. Never silently overwrite description text.

## Phase 4 — Occupation classification

Classify each posting to one or more O*NET-SOC codes. Use a **tiered cascade**, and record which tier fired.

1. **Exact/normalized title match** against O*NET titles and alternate titles — confidence 0.90.
2. **Fuzzy title match** (trigram similarity ≥ threshold, tuned on a labeled set) — confidence 0.70–0.85, scaled by similarity.
3. **Embedding similarity** between posting title + first ~500 chars of description and occupation title + description, via pgvector — confidence 0.50–0.70.
4. **LLM classification** with the top-5 embedding candidates supplied as constrained choices, forced to either pick one or return `no_confident_match`. Never let it invent a SOC code — validate the returned code against `occupations` and reject otherwise.

- Write to `posting_occupations`: `posting_id`, `onet_soc_code`, `confidence` (0–1 numeric), `method` (enum for the tier), `model_version`, `classified_at`. Allow multiple rows per posting; mark one `is_primary`.
- Build a labeled evaluation set of ~200 postings I can hand-label. Report precision/recall per tier and a confusion summary of the most-confused occupation pairs. **Do not tune thresholds without this set** — hardcode conservative defaults and tell me they're provisional.

## Phase 5 — Requirement extraction with evidence

For each posting description, extract structured requirements. Every extraction carries a verbatim excerpt.

`posting_requirements` table: `requirement_id`, `posting_id`, `req_type` (enum: `major` | `degree_level` | `credential` | `certification` | `license` | `skill` | `tool` | `experience_years` | `clearance` | `other`), `value_raw`, `value_normalized`, `is_required` (true) vs preferred (false) vs unknown (null), `evidence_excerpt` (verbatim, ≤300 chars), `evidence_start_offset`, `evidence_end_offset`, `extraction_method`, `confidence`, `model_version`, `extracted_at`.

- Hybrid approach: deterministic patterns first (degree levels, "X+ years", named certifications from a controlled list, security clearance terms), then LLM extraction for the residue.
- **Enforce the evidence contract in code**: post-validate that `description_raw[start:end] == evidence_excerpt` exactly. Drop any extraction that fails this check and log it. This is the single most important guardrail in the pipeline — an extraction without verifiable evidence is a hallucination.
- Distinguish required vs preferred by section heading and cue phrases ("must have", "required" vs "nice to have", "preferred", "bonus"). Store the cue in `extraction_method` so I can audit it.
- Normalize `experience_years` into `min_years` / `max_years` numerics in a separate `posting_experience` table — ranges, "5+", and "3-5" all need to be queryable.
- Normalize degree levels to a small controlled vocabulary: `associate`, `bachelor`, `master`, `doctorate`, `professional`, `bootcamp_certificate`, `hs_diploma`.

## Phase 6 — Major linkage (explicit and inferred)

Two distinct paths into `posting_majors`, and they must stay distinguishable forever.

`posting_majors`: `posting_id`, `cip_code`, `link_type` (`explicit_text` | `crosswalk_inferred`), `confidence`, `evidence_requirement_id` (FK, nullable — populated for explicit only), `via_onet_soc_code` (nullable — populated for inferred only), `linked_at`, `model_version`. Composite PK on (posting_id, cip_code, link_type).

- **Explicit path**: take `req_type = 'major'` extractions and map the free-text major string to CIP codes. Build a `major_alias` table (e.g. "CS", "Computer Science", "Comp Sci", "EECS" → `11.0701`) seeded from CIP titles plus a hand-curated alias file in `data/reference/major_aliases.csv`. Confidence 0.80–0.95. Map to the most specific CIP that the text supports — "Engineering" maps to the 2-digit family, not to a guessed 6-digit code.
- **Inferred path**: for each `posting_occupations` row, join `cip_soc_crosswalk` to get candidate CIPs. Confidence = `posting_occupation.confidence × crosswalk_prior`, capped at **0.60** so an inferred link can never outrank an explicit one. `crosswalk_prior` should be lower when the SOC maps to many CIPs (a SOC mapping to 30 majors is weak evidence for any one of them) — implement as `1 / log2(2 + fan_out)` or similar, and document the choice.
- Default all user-facing queries to explicit links, with inferred available behind an opt-in flag. Add a `SET` of example queries in `scripts/example_queries.sql` demonstrating both.

## Phase 7 — Freshness and staleness

- Scheduler stage `pipeline.refresh` that re-checks active postings: **daily** for sources that expose a cheap listing endpoint, **weekly** otherwise. Make cadence a per-source config value.
- Refresh logic: if the posting is still in the source listing → bump `last_seen_at`. If absent for N consecutive checks (default 2) → set `status = 'closed'`, `closed_at = now()`. If absent but the source itself failed to fetch → do **not** close; log a `crawl_runs` failure instead. Never close records because of an outage.
- Add `status = 'stale'` for postings whose `last_seen_at` exceeds a configurable window (default 45 days) without an explicit close signal.
- `crawl_runs` table: `run_id`, `source_id`, `started_at`, `finished_at`, `postings_seen`, `postings_new`, `postings_closed`, `errors_count`, `status`. Emit one row per source per run.
- Closed postings are **retained**, not deleted — historical demand is the point of the dataset. Every analytical query must filter on status explicitly; no default that hides history.

## Phase 8 — Human review queue

`review_items` table: `review_id`, `entity_type` (`posting_occupation` | `posting_major` | `posting_requirement` | `company_merge`), `entity_id`, `reason` (enum), `priority` (numeric), `status` (`pending` | `approved` | `rejected` | `corrected`), `reviewer_note`, `corrected_value` (JSONB), `reviewed_by`, `reviewed_at`.

Enqueue on:
- confidence below a per-type threshold (start: 0.60 occupation, 0.70 explicit major, 0.50 requirement)
- evidence-offset validation failures
- ambiguous company merges
- **high-traffic results** — add a `query_views` counter table and enqueue the top-viewed (cip_code, company_id) pairs for audit regardless of confidence

Build a minimal review UI: a FastAPI + HTMX single-page queue showing the evidence excerpt, the source posting link, the proposed mapping, and approve/reject/correct buttons. Corrections must write back to the underlying table **and** append to a `corrections_log` I can use as training data later.

---

## Deliverables checklist

Status as of the D1 migration and the transcript/resume service. Phases 1–3 are
done; 4–8 are not started.

- [x] ~~Alembic migrations for the full schema~~ → `migrations/d1/*.sql`, generated
      from the ORM by `scripts/generate_d1_schema.py` and kept honest by
      `tests/test_schema_parity.py`. Alembic is gone with Postgres.
- [x] Idempotent, re-runnable CLI stage per phase — for the phases that exist
      (`load_cip`, `load_occupations`, `load_cip_soc_crosswalk`, `load_sources`,
      `ingest_postings`, `init_db`, `sync_d1`)
- [x] `data/reference/SOURCE.md` documenting every reference file's URL, date, and hash
- [x] Coverage report for the CIP–SOC crosswalk — `scripts/crosswalk_coverage.py`
- [ ] Labeled eval set + precision/recall report for Phase 4
- [ ] Evidence-offset validation test that fails loudly — **the posting-extraction
      one is Phase 5 and not written.** The same guardrail exists on the resume
      side today (`service/resume_evidence.py`, anchored on profile ids rather than
      character offsets).
- [ ] `scripts/example_queries.sql` with the five queries below working end to end —
      needs Phases 4–6
- [x] `README.md` with setup, credentials expected in `.env`, and how to add a new source adapter

## Acceptance queries

These must run and return sensible results:

1. Companies hiring for CIP `11.0701` (Computer Science) in the last 90 days, ranked by active posting count.
2. Top 25 credentials/certifications requested for a given CIP, with counts and one example evidence excerpt each.
3. Distribution of `min_years` experience for a given CIP, split by degree level.
4. Explicit vs inferred link counts per CIP — so I can see where the data is real versus extrapolated.
5. Postings for a CIP where the major was named explicitly in the text, with the verbatim sentence shown.

## What to ask me before starting

Flag anything here that you need from me rather than guessing:
- which licensed feed (if any) I have access to, and its credentials
- the initial company/ATS allowlist
- whether to include internships and part-time roles, or full-time only
- LLM provider and model for the extraction and classification stages

Start with Phase 1. Show me the migration and the loaded row counts before moving on.
