"""FastAPI entrypoint. Run with: uvicorn service.app:app --reload --port 8000

Three endpoints do the work, and the split between them is the privacy boundary:

    POST /api/transcript/parse   reads an uploaded PDF and returns structured
                                 fields, storing nothing
    POST /api/student/profile    stores fields the student has reviewed — the
                                 only write in the service
    POST /api/resume/generate    drafts a resume from a reviewed profile plus
                                 hiring demand, storing nothing

A transcript can never reach the database without a student explicitly sending
it back. `GET /health` is liveness only.

`frontend/` is the browser client for these three. With SERVE_FRONTEND set it
is served from this app, which puts it on the same origin and takes CORS out of
the picture entirely; otherwise serve it however you like and list its origin in
PATHFINDER_ALLOWED_ORIGINS.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from service import llm_extraction, profile_store, resume_generation
from service.config import (
    FRONTEND_DIR,
    MAX_TRANSCRIPT_TEXT_CHARS,
    LLM_EXTRACTION_ENABLED,
    LLM_MODEL,
    MAX_UPLOAD_BYTES,
    RESUME_MAX_VARIANT,
    RESUME_MODEL,
    SERVE_FRONTEND,
)
from service.resume_evidence import run_match
from service.schemas import (
    GenerateResumeRequest,
    GenerateResumeResponse,
    ParseResponse,
    ParseTextRequest,
    SaveProfileRequest,
    SaveProfileResponse,
)
from service.transcript_service import parse_transcript_bytes, parse_transcript_text

log = structlog.get_logger()

# Set by the Worker entrypoint (see `worker/entry.py`). Left None locally, which
# is what makes /api/roles/search a no-op outside the deployed environment.
_role_search = None


def use_role_search(backend) -> None:
    """Register the occupation-index reader. Called once, at Worker startup."""
    global _role_search
    _role_search = backend


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Fail loudly in logs rather than silently serving degraded results: a
    deploy with the LLM stage on but no credentials still works, but every
    upload quietly falls back to regex extraction."""
    if not LLM_EXTRACTION_ENABLED:
        log.info("service.extraction_mode", mode="rules", reason="LLM_EXTRACTION_ENABLED is off")
    elif llm_extraction.is_enabled():
        log.info("service.extraction_mode", mode="llm", model=LLM_MODEL)
    else:
        log.error(
            "service.extraction_mode",
            mode="rules",
            reason=(
                "LLM extraction is enabled but no ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN "
                "is present in the environment — every upload will fall back to regex extraction"
            ),
        )

    # Resume drafting has no fallback stage, so an unavailable one is a feature
    # being off rather than a quality downgrade — worth its own line.
    if resume_generation.is_enabled():
        log.info("service.resume_mode", mode="llm", model=RESUME_MODEL)
    else:
        log.warning(
            "service.resume_mode",
            mode="unavailable",
            reason=(
                "RESUME_GENERATION_ENABLED is off or no ANTHROPIC_API_KEY/"
                "ANTHROPIC_AUTH_TOKEN is present — POST /api/resume/generate will "
                "return success=False"
            ),
        )
    yield


app = FastAPI(title="Transcript Extraction Service", lifespan=lifespan)

# Set PATHFINDER_ALLOWED_ORIGINS to a comma-separated list of your frontend
# origins in production. "*" is a development default and must not ship.
_origins = [o.strip() for o in os.getenv("PATHFINDER_ALLOWED_ORIGINS", "*").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    # GET is here for /health and for the static files when SERVE_FRONTEND is
    # off and something else fronts them. Everything that carries student data
    # is a POST.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.post("/api/transcript/parse", response_model=ParseResponse)
async def parse_transcript(file: UploadFile = File(...)) -> ParseResponse:
    """Extract structured academic fields from an uploaded transcript PDF.

    Persists nothing: not the PDF, not its extracted text, not the returned
    profile. Every response carries review_required=True; storing the result is
    a separate, explicit call to /api/student/profile.
    """
    # Read with a hard cap so an oversized upload can't exhaust memory before
    # our own size validation ever runs.
    chunks = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large.")
        chunks.append(chunk)
    content = b"".join(chunks)

    return await parse_transcript_bytes(
        filename=file.filename or "",
        content_type=file.content_type,
        content=content,
    )


@app.post("/api/transcript/parse-text", response_model=ParseResponse)
async def parse_transcript_from_text(request: ParseTextRequest) -> ParseResponse:
    """Same extraction, but from text the browser already pulled out of the PDF.

    This is the path the deployed Worker serves: MarkItDown has native
    dependencies and cannot run there, and the browser already ships a pdf.js
    extractor for its offline fallback. It is also the *less* exposed of the
    two — the PDF bytes never leave the student's machine, only its text does.

    Persists nothing, exactly like the upload endpoint.
    """
    if len(request.text) > MAX_TRANSCRIPT_TEXT_CHARS:
        raise HTTPException(status_code=413, detail="Document text too large.")
    return await parse_transcript_text(request.text)


@app.post("/api/student/profile", response_model=SaveProfileResponse)
async def save_student_profile(request: SaveProfileRequest) -> SaveProfileResponse:
    """Store a profile the student has reviewed.

    `model_version` is recorded only for the LLM path — the rule-based and
    manual paths have no model behind them, and claiming one would be a
    provenance lie.
    """
    model_version = LLM_MODEL if request.extraction_method == "llm" else None

    profile_id, courses, attributes = await profile_store.save_profile_async(
        request.academic_profile,
        extraction_method=request.extraction_method,
        model_version=model_version,
    )
    return SaveProfileResponse(
        student_profile_id=str(profile_id),
        courses_saved=courses,
        attributes_saved=attributes,
    )


@app.post("/api/resume/generate", response_model=GenerateResumeResponse)
async def generate_resume(request: GenerateResumeRequest) -> GenerateResumeResponse:
    """Draft a resume from a reviewed profile plus real hiring demand.

    Persists nothing. The profile arrives on the request, is used to build the
    resume, and is gone when the response is sent — storing a profile is the
    separate, explicit /api/student/profile call.

    Always returns HTTP 200. A disabled stage, a refusal, or an API outage is
    `success=False` with a student-facing warning, because a career tool that
    500s mid-flow loses the student's typed-in experience along with it.
    """
    # The match is always recomputed here rather than accepted from the client:
    # the browser mirrors the matcher in JS but assigns its own evidence ids, and
    # ids that don't resolve server-side would drop every claim.
    match = run_match(
        request.academic_profile,
        request.market_profile,
        request.experience,
        request.projects,
    )

    if not resume_generation.is_enabled():
        log.warning("resume.unavailable", reason="stage disabled or no credentials")
        return GenerateResumeResponse(
            success=False,
            gaps=match.gaps,
            warnings=[
                "Resume drafting is temporarily unavailable. Your profile has been "
                "kept — please try again shortly."
            ],
            variant=request.variant,
        )

    # Bound the "regenerate wording" counter so a client cannot push an
    # arbitrary integer into the prompt.
    variant = max(0, min(int(request.variant), RESUME_MAX_VARIANT))

    try:
        document, dropped, warnings = await resume_generation.generate_resume(
            career=request.career,
            profile=request.academic_profile,
            experience=request.experience,
            projects=request.projects,
            market_profile=request.market_profile,
            market_match=match,
            variant=variant,
        )
    except resume_generation.ResumeGenerationError as exc:
        # Reason only — never the profile text or the model's output.
        log.warning("resume.generation_failed", reason=str(exc))
        return GenerateResumeResponse(
            success=False,
            gaps=match.gaps,
            warnings=[
                "We couldn't draft a resume just now. Your profile has been kept — "
                "please try again."
            ],
            variant=variant,
        )

    return GenerateResumeResponse(
        success=True,
        # Flat mirror so the existing browser caller, which destructures
        # `{ summary }`, keeps working against the real endpoint unchanged.
        summary=document.summary.text,
        resume=document,
        dropped=dropped,
        gaps=match.gaps,
        warnings=warnings,
        model_version=RESUME_MODEL,
        variant=variant,
    )


@app.get("/api/roles/search")
async def search_roles(q: str = "") -> dict:
    """Autocomplete a career target against the O*NET occupation index.

    Real data, but only where a backend is registered: the pipeline publishes
    `occupations` and `occupation_alt_titles` to D1, and the Worker wires a
    reader over them at startup. Running locally with no backend this returns
    an empty list, and the browser falls back to its own mock — the one place
    the deployed app is *more* real than the local one.
    """
    if _role_search is None:
        return {"results": []}
    try:
        return {"results": await _role_search.search(q)}
    except Exception as exc:
        # Autocomplete is an assist, not a gate. A missing index (a dev replica
        # with no tables) or a transient D1 error degrades to the browser's own
        # short list rather than failing the keystroke.
        log.warning("roles.search_failed", reason=type(exc).__name__)
        return {"results": []}


@app.get("/health")
async def health() -> dict:
    """`async`, and every other route here is too, deliberately.

    FastAPI dispatches a *sync* `def` endpoint to a threadpool. Cloudflare
    Workers have no threads, so a sync route 500s there while every async one
    beside it works — a failure that looks like a broken endpoint rather than a
    broken execution model. Keep new routes async.
    """
    return {"status": "ok"}


# Mounted last so it cannot shadow a route: a mount at "/" matches every path
# the routes above did not claim. `html=True` serves index.html for "/".
#
# There is no build step — the client is Alpine plus two vendored libraries, so
# the directory is served as it sits in the repo.
if SERVE_FRONTEND:
    if not FRONTEND_DIR.is_dir():
        raise RuntimeError(
            f"SERVE_FRONTEND is set but {FRONTEND_DIR} does not exist. Unset it to run "
            "the API alone."
        )
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    log.info("service.frontend", mode="served", directory=str(FRONTEND_DIR))
