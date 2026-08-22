"""Service-wide constants. No secrets here — safe to import anywhere.

The Anthropic API key is NEVER read here or stored in this repo. The SDK resolves
it from the environment at call time (`ANTHROPIC_API_KEY`), which is where a cloud
secret manager should inject it. `.env` is loaded the same way `models.db` loads
it, so local development needs no extra wiring.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env", override=True)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB hard cap
ALLOWED_EXTENSION = ".pdf"
ALLOWED_CONTENT_TYPES = {"application/pdf"}
PDF_MAGIC_BYTES = b"%PDF-"

# Heuristic: MarkItDown output shorter than this from a "normal" page count
# strongly suggests a scanned/image-only PDF with no extractable text layer.
MIN_EXTRACTED_CHARS = 40

# Upper bound on browser-extracted transcript text (POST /api/transcript/parse-text).
# The upload path is bounded by MAX_UPLOAD_BYTES; this is the equivalent ceiling
# for the path where the browser did the extraction. Generous enough for a long
# multi-page transcript, small enough that a single request cannot be used to
# push arbitrary bulk through the service.
MAX_TRANSCRIPT_TEXT_CHARS = 400_000


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Claude API extraction -------------------------------------------------
# Kill switch: turn the LLM stage off without a redeploy and the service falls
# back to the rule-based normalizer.
LLM_EXTRACTION_ENABLED = _env_flag("LLM_EXTRACTION_ENABLED", True)

LLM_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")

# Effort is the main quality/cost/latency dial. `medium` is a solid default for
# structured extraction; sweep low/medium/high against your own transcripts.
LLM_EFFORT = os.getenv("ANTHROPIC_EFFORT", "medium")

# Caps thinking *and* response tokens together, and on this model thinking is on
# by default — so this needs headroom for both. A long transcript's course list
# alone can run a few thousand output tokens; hitting the cap surfaces as
# stop_reason=max_tokens, which downgrades the request to regex extraction.
LLM_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "32000"))

# Per-attempt HTTP timeout, and retries on top of it. Worst-case wall clock is
# LLM_TIMEOUT_SECONDS * (LLM_MAX_RETRIES + 1) — keep it under your ingress /
# load-balancer request timeout.
LLM_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "60"))
LLM_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "1"))

# Upper bound on document text handed to the model (~100k tokens). Anything
# longer is truncated *with an explicit warning to the student* — never silently.
MAX_LLM_INPUT_CHARS = int(os.getenv("MAX_LLM_INPUT_CHARS", "400000"))


# --- Claude API resume generation ------------------------------------------
# Same dials as the extraction stage, separately tunable: the two calls have
# different shapes (a long document in / small object out, vs. a small profile
# in / a whole resume out) and want different budgets.

# Kill switch. Off means POST /api/resume/generate returns success=False with a
# student-facing warning — there is no rule-based fallback for a resume, and
# writing one would mean generating prose without a model, which is exactly the
# template-mush this stage exists to replace.
RESUME_GENERATION_ENABLED = _env_flag("RESUME_GENERATION_ENABLED", True)

RESUME_MODEL = os.getenv("RESUME_MODEL", LLM_MODEL)

# Drafting a resume from an already-structured profile is not a deep-reasoning
# task. `medium` is the starting point; sweep low/medium/high against real
# profiles before raising it, since this runs on every "regenerate" click.
RESUME_EFFORT = os.getenv("RESUME_EFFORT", "medium")

# Caps thinking and response together. A resume with many experiences and
# projects is a few thousand output tokens; the headroom above that is for
# thinking, which is on by default on this model.
RESUME_MAX_TOKENS = int(os.getenv("RESUME_MAX_TOKENS", "16000"))

RESUME_TIMEOUT_SECONDS = float(os.getenv("RESUME_TIMEOUT_SECONDS", "120"))
RESUME_MAX_RETRIES = int(os.getenv("RESUME_MAX_RETRIES", "1"))

# Bounds the "regenerate wording" button so a client cannot ask for variant
# 10_000 and get an unbounded integer into the prompt. `frontend/js/app.js`
# clamps to the same value so the counter it shows the student stays honest;
# change both together.
RESUME_MAX_VARIANT = int(os.getenv("RESUME_MAX_VARIANT", "9"))


# --- Frontend --------------------------------------------------------------
# Off by default: this is an API service, and a deploy that fronts it with a CDN
# or a separate static host should not also be serving files. On, it mounts
# `frontend/` at "/", which puts the browser client on the same origin as the
# API — no CORS, and `TRANSCRIPT_SERVICE_URL` stays empty. That is the simplest
# way to run the whole product locally.
SERVE_FRONTEND = _env_flag("SERVE_FRONTEND", False)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
