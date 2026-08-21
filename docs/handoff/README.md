# Design handoff

`Stellic Pathfinders Career Application-handoff.zip` is the original design handoff
this project's demand side was built from. Source material, not code — nothing in the
repo imports it and nothing builds from it.

It contains a design prototype (`Career Pathfinder.dc.html`) plus a first-pass
`backend/` and three JS service modules. Two things in this repo derive from it:

- `frontend/` is a port of the prototype's `DCLogic` component to Alpine, with its
  `api-service.js`, `academic-extraction.js`, and `course-catalog.js` carried over.
- `service/` began as its `backend/` directory, renamed and since rewritten —
  `llm_extraction.py`, `resume_generation.py`, `resume_evidence.py`, and
  `profile_store.py` have no counterpart in the zip.

Kept for provenance: when a comment or a data shape in `frontend/` looks like it came
from nowhere, it probably came from here. Do not treat it as current — where the zip
and this repo disagree, the repo is right.
