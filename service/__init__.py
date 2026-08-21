"""Transcript extraction service.

An uploaded academic PDF is validated, converted to text, and turned into a
structured `AcademicProfile` — primarily by the Claude API (`llm_extraction`),
falling back automatically to the rule-based normalizer (`academic_extraction`)
whenever the API stage cannot produce a usable result.

This package is deliberately separate from `pipeline/`: those are batch stages
run as `python -m pipeline.<stage>`, whereas this is a long-running HTTP service
(`uvicorn service.app:app`). The only thing it shares with the pipeline is the
database, through `models/` and `service.profile_store`.

Nothing here is imported by any pipeline stage, so the pipeline keeps running
without an Anthropic key and without the web dependencies loaded.
"""
