"""Cloudflare Worker entrypoint for the Pathfinder service.

This file is the *only* thing that differs between running locally and running
on the edge. Everything it serves — extraction, the evidence contract, the
matcher — is the same `service/` code the local uvicorn process runs; this
module just supplies the two things Workers do differently:

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

import asgi
from js import URL
from workers import WorkerEntrypoint

from service.app import app, use_role_search
from service.d1_store import D1ProfileStore, D1RoleSearch
from service.profile_store import use_backend

_wired = False


def _wire(env) -> None:
    """Bind the D1-backed stores once per isolate.

    Done on first request rather than at import: the bindings live on `env`,
    which only exists inside a request context. The flag keeps it to one pass —
    re-registering is harmless but pointless, and this runs on a hot path.
    """
    global _wired
    if _wired:
        return
    use_backend(D1ProfileStore(env.DB))
    use_role_search(D1RoleSearch(env.DB))
    _wired = True


# Paths FastAPI owns. Everything else is the browser client and is served by
# Workers Static Assets. The split is explicit rather than "try Python, fall
# back on 404" so a genuine 404 from an API route stays a 404 instead of
# silently returning index.html.
API_PREFIXES = ("/api/", "/health", "/docs", "/openapi.json", "/redoc")


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        path = URL.new(request.url).pathname

        if not path.startswith(API_PREFIXES):
            # `run_worker_first` in wrangler.jsonc routes every request here, so
            # handing the static ones back to the assets binding is this
            # Worker's job, not something the platform does on its own.
            return await self.env.ASSETS.fetch(request)

        _wire(self.env)
        return await asgi.fetch(app, request, self.env)
