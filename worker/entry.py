"""Cloudflare Worker entrypoint for the Pathfinder service.

This file is the *only* thing that differs between running locally and running
on the edge. Everything it serves — extraction, the evidence contract, the
matcher — is the same `service/` code the local uvicorn process runs; this
module just supplies the three things Workers do differently:

- **Configuration.** Worker vars and secrets arrive on `env`, not in
  `os.environ`, and `service/config.py` reads its constants at *import* time.
  So the environment is hydrated first and `service` is imported second. Get
  that order wrong and every setting silently falls back to its default — which
  looks like a working deploy that quietly never calls the model.
- **Storage.** No filesystem database exists here, so the profile store is
  pointed at the D1 binding instead of SQLAlchemy.
- **Reads.** The occupation index the pipeline published to D1 becomes a real
  autocomplete, replacing the browser's mock.

What it deliberately does *not* do is re-implement any endpoint. If a behavior
differs between local and deployed, that is a bug in this file, not a feature.

Not served here: `POST /api/transcript/parse` (the multipart upload path). It
needs MarkItDown, which has native dependencies and cannot run on Pyodide, so
the browser extracts the PDF text with pdf.js and posts it to
`/api/transcript/parse-text` instead. Same extraction, same rules, and the PDF
bytes never leave the student's machine.
"""

import os

import asgi
from js import URL
from workers import WorkerEntrypoint

# Every name `service/` reads out of the environment. Listed explicitly rather
# than copied wholesale off `env`: this is the contract between wrangler.jsonc
# and `service/config.py`, and it should be readable in one place. A name absent
# from `env` is left alone so the config default applies.
#
# ANTHROPIC_API_KEY is the load-bearing one — the SDK resolves it from
# `os.environ` itself, so without this bridge the deploy runs in rules mode with
# a perfectly healthy-looking startup log.
CONFIG_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_EFFORT",
    "ANTHROPIC_MAX_TOKENS",
    "ANTHROPIC_TIMEOUT_SECONDS",
    "ANTHROPIC_MAX_RETRIES",
    "LLM_EXTRACTION_ENABLED",
    "MAX_LLM_INPUT_CHARS",
    "RESUME_GENERATION_ENABLED",
    "RESUME_MODEL",
    "RESUME_EFFORT",
    "RESUME_MAX_TOKENS",
    "RESUME_TIMEOUT_SECONDS",
    "RESUME_MAX_RETRIES",
    "RESUME_MAX_VARIANT",
    "PATHFINDER_ALLOWED_ORIGINS",
)

# Paths FastAPI owns. Everything else is the browser client and is served by
# Workers Static Assets. The split is explicit rather than "try Python, fall
# back on 404" so a genuine 404 from an API route stays a 404 instead of
# silently returning index.html.
API_PREFIXES = ("/api/", "/health", "/docs", "/openapi.json", "/redoc")

_app = None


def _boot(env):
    """Hydrate the environment, import the service, bind the D1 stores.

    Runs once per isolate, on the first request — `env` does not exist at
    module import time, which is the whole reason the service import is in here
    rather than at the top of the file.
    """
    global _app
    if _app is not None:
        return _app

    for name in CONFIG_NAMES:
        value = getattr(env, name, None)
        if value is not None:
            os.environ[name] = str(value)

    # Imported only now, so `service/config.py` reads the environment above
    # rather than an empty one.
    from service.app import app, use_role_search
    from service.d1_store import D1ProfileStore, D1RoleSearch
    from service.profile_store import use_backend

    use_backend(D1ProfileStore(env.DB))
    use_role_search(D1RoleSearch(env.DB))

    _app = app
    return app


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = URL.new(request.url).pathname

        if not path.startswith(API_PREFIXES):
            # `run_worker_first` in wrangler.jsonc routes every request here, so
            # handing the static ones back to the assets binding is this
            # Worker's job, not something the platform does on its own.
            return await self.env.ASSETS.fetch(request)

        return await asgi.fetch(_boot(self.env), request, self.env)
