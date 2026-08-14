# Reference data sources

## Phase 3 source seed (`sources_seed.csv`)

- **File**: `sources_seed.csv`
- **What it is**: seed rows for the `sources` table, not a third-party download —
  hand-curated, so there's no upstream URL/hash to record. Documenting the
  provenance of the *choices* instead:
- **Greenhouse boards (14 rows, `source_type=ats_api`, `enabled=true`)**: public
  `boards-api.greenhouse.io` job-board API, which the project's hard constraints
  pre-clear as ToS-clean for board data (documented at
  https://developers.greenhouse.io/job-board.html). Each board token
  (`stripe`, `airbnb`, `figma`, `coinbase`, `reddit`, `discord`, `instacart`,
  `asana`, `cloudflare`, `databricks`, `robinhood`, `lyft`, `pinterest`,
  `dropbox`) was verified live with a direct `GET .../v1/boards/{token}/jobs`
  request on 2026-08-14 before being added — a token that 404s isn't seeded.
  `terms_reviewed_at` is set to that verification date. This is a starter list
  for exercising the pipeline end to end; extend or trim it freely.
- **Handshake (1 row, `source_type=licensed_feed`, `enabled=false`)**: seeded as
  a disabled stub only. Handshake's employer-side API is partner/credentialed
  access, not a self-serve public endpoint like Greenhouse's — per hard
  constraint 1(a), it can't be wired up without real credentials and a look at
  its actual auth flow and rate limits. `base_url` points at Handshake's known
  API host (`api.joinhandshake.com`) as a placeholder; `auth_mode=api_key` is a
  guess pending real docs. **Needed to build this adapter for real**: API
  credentials and the relevant API/partner documentation.

## CIP 2020 code list

- **File**: `CIPCode2020.csv`
- **URL**: https://nces.ed.gov/ipeds/cipcode/resources.aspx?y=56 (NCES IPEDS CIP User Site,
  "CIP2020" → Excel/CSV export via the `LinkButton_CurrentExcel` control; the page itself
  serves the file as `CIPCode2020.csv`, `Content-Type: application/vnd.ms-excel`)
- **Retrieved at**: 2026-08-12T01:42:28Z
- **SHA-256**: `6cf0882c1f5beb94981d0a1a72285ab5cf633759f45433fb909afbfb6d6b2657`
- **Columns**: `CIPFamily, CIPCode, Action, TextChange, CIPTitle, CIPDefinition,
  CrossReferences, Examples`
- **Notes**:
  - `CIPCode` values are Excel-formula-escaped (`="01"`, `="01.0101"`) to preserve
    leading zeros; the loader strips the `="..."` wrapper.
  - Row granularity is mixed: `CIPCode` length/format distinguishes the level —
    `XX` = 2-digit family, `XX.XX` = 4-digit series, `XX.XXXX` = 6-digit program.
    Some 4-digit series rows are placeholders whose `CIPDefinition` reads
    "Instructional content is defined in code ..." when the series has a single
    6-digit child — these are still loaded as real rows (the spec requires all
    three levels present and joinable).
  - `Action` / `TextChange` carry the CIP2010→CIP2020 revision notes (e.g. "No
    substantive changes", "Moved from...", "New CIP code..."). These are preserved
    verbatim into `cip_codes.crosswalk_notes` per the spec's instruction to keep
    "moved from / new in 2020" notes for reconciling older data.

## O*NET-SOC occupations + alternate titles

- **Files**: `onet_occupation_data.csv`, `onet_job_titles.csv`, `onet_job_zones.csv`
- **URL**: https://www.onetcenter.org/database.html — O*NET 30.3 Database (May 2026
  release), CSV bundle at `https://www.onetcenter.org/dl_files/database/db_30_3_csv.zip`,
  files extracted: `occupation_data.csv`, `job_titles.csv`, `job_zones.csv`
- **Retrieved at**: 2026-08-12T09:55:44Z
- **SHA-256** (of the extracted files actually checked in):
  - `onet_occupation_data.csv`: `a09eae1d6609686e44e05b7290993a1c8b523d8ca224bc0eedc194c855c3ee02`
  - `onet_job_titles.csv`: `db3ac1f2519d8e59df9c727e4eb9cc8096bd196f111284f908be1758b267dbc2`
  - `onet_job_zones.csv`: `075782b37d63dfa37d8afa6dea975388350baf2f4d1906e39b66766a05d72ba8`
- **License**: O*NET database content is CC BY 4.0 (https://www.onetcenter.org/license_db.html).
- **Columns**:
  - `onet_occupation_data.csv`: `O*NET-SOC Code, Title, Description` (1,016 rows — one
    per O*NET-SOC detailed occupation)
  - `onet_job_titles.csv`: `O*NET-SOC Code, Title, Job Title, Short Title, Source(s)`
    (57,543 rows — this is O*NET's "Alternate Titles"-equivalent file, named
    `job_titles.csv` in the 30.3 release; no duplicate `(O*NET-SOC Code, Job Title)`
    pairs)
  - `onet_job_zones.csv`: `O*NET-SOC Code, Title, Job Zone, Date, Domain Source`
    (923 rows — not every occupation has an assigned job zone)
- **Notes**:
  - `soc_2018_code` (the 6-digit SOC parent) is **derived**, not a source column:
    it's the O*NET-SOC code with the `.XX` detail suffix stripped
    (`11-1011.00` → `11-1011`). This is O*NET's documented code structure, not an
    inference — O*NET-SOC codes are SOC 2018 codes extended with a 2-digit suffix
    for detailed occupations.
  - `bright_outlook` is **not populated in this phase**: it isn't included in the
    core O*NET database bundle (only via the O*NET Web Services API, which needs
    registration/credentials). Column is nullable and left `NULL`; revisit if a
    later phase needs it.

## CIP 2020 – SOC 2018 crosswalk

- **File**: `CIP2020_SOC2018_Crosswalk.xlsx`
- **URL**: https://nces.ed.gov/ipeds/cipcode/Files/CIP2020_SOC2018_Crosswalk.xlsx
  (official NCES/IPEDS crosswalk, linked from the CIP 2020 resources page)
- **Retrieved at**: 2026-08-12T09:56:26Z
- **SHA-256**: `ba3d59a191b9d977a5c457a66b9348c4f2f7963aafacf72c0b80113b46bf0ab8`
- **Sheet used**: `CIP-SOC` — columns `CIP2020Code, CIP2020Title, SOC2018Code,
  SOC2018Title`, 6,097 pairs across 2,143 unique CIP codes and 868 unique SOC2018
  codes. (`SOC-CIP` is the same edge set sorted the other way — not separately
  loaded, it would only duplicate the same pairs.)
- `crosswalk_source` value used in `cip_soc_crosswalk`: `nces_cip_soc_2020`.
- **Notes**:
  - The crosswalk operates at **SOC 2018** granularity (6-digit, e.g. `19-1011`),
    not O*NET-SOC granularity. The loader fans each `(cip_code, soc_2018_code)`
    pair out to every `occupations` row sharing that `soc_2018_code` (derived as
    above), producing the `(cip_code, onet_soc_code)` pairs the spec's
    `cip_soc_crosswalk` table expects.
  - NCES uses `99.9999` / `99-9999` as placeholder codes to represent "no real
    counterpart exists" on either side: 149 CIP codes appear in the `CIP-SOC`
    sheet paired with the placeholder SOC `99-9999` (no matching occupation), and
    191 rows pair a real SOC with the placeholder CIP `99.9999` (no matching
    major). Neither placeholder exists in `cip_codes` / `occupations`, so both
    are dropped with a logged warning rather than failing the load — this is
    exactly the kind of gap the coverage report should surface, not something to
    silently paper over.
