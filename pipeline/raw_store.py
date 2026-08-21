"""Content-addressed object store for raw payload bodies.

`raw_documents` rows keep only metadata plus a key into this store. Bodies live
outside the database because D1 caps a row at 2 MB and a database at 10 GB, and raw
job descriptions are the one table here that grows without bound.

Keys are derived purely from the payload's SHA-256, so writing the same bytes twice
produces the same key with the same contents. That makes `put()` idempotent and
preserves the "raw documents are never mutated" contract in `models/raw_document.py`
without needing any read-before-write check.

Two backends, selected by whether R2 credentials are present:

- `R2Store`     — Cloudflare R2 over its S3-compatible API. Production.
- `LocalStore`  — a directory under `data/raw_store/`. Used when R2 credentials are
                  absent, so the test suite and offline development need no network
                  and no cloud account.

Both satisfy the same interface, and the key for a given payload is identical in
either, so a local store can be uploaded to R2 later without rewriting a single row.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional, Protocol

import structlog

log = structlog.get_logger()

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_STORE_DIR = _REPO_ROOT / "data" / "raw_store"


def payload_sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def key_for(sha256: str) -> str:
    """Content-addressed key. The two-character prefix keeps any single directory
    (or R2 list page) from accumulating every object in the corpus."""
    return f"raw/{sha256[:2]}/{sha256}"


class RawStore(Protocol):
    def put(self, payload: str, sha256: str) -> str: ...
    def get(self, key: str) -> str: ...


class LocalStore:
    """Filesystem-backed store. Same keys as R2, rooted at `data/raw_store/`."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, payload: str, sha256: str) -> str:
        key = key_for(sha256)
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        # Content-addressed: if it exists, it already holds exactly these bytes.
        if not path.exists():
            path.write_text(payload, encoding="utf-8")
        return key

    def get(self, key: str) -> str:
        return (self.root / key).read_text(encoding="utf-8")


class R2Store:
    """Cloudflare R2 via its S3-compatible API."""

    def __init__(self, account_id: str, bucket: str, access_key_id: str, secret_access_key: str) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    def put(self, payload: str, sha256: str) -> str:
        key = key_for(sha256)
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=payload.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        return key

    def get(self, key: str) -> str:
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read().decode("utf-8")


@lru_cache(maxsize=1)
def get_store() -> RawStore:
    """Return the R2 store when credentials are configured, else the local store.

    Falling back rather than failing is deliberate: every stage stays runnable, and
    the whole test suite stays offline, without R2 access. The choice is logged so a
    run that silently wrote to disk when it was meant to reach R2 is visible.
    """
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    bucket = os.environ.get("R2_BUCKET", "").strip()
    access_key_id = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()

    if account_id and bucket and access_key_id and secret_access_key:
        log.info("raw_store.backend", backend="r2", bucket=bucket)
        return R2Store(account_id, bucket, access_key_id, secret_access_key)

    log.info("raw_store.backend", backend="local", path=str(_LOCAL_STORE_DIR))
    return LocalStore(_LOCAL_STORE_DIR)


def put(payload: str, sha256: Optional[str] = None) -> tuple[str, str, int]:
    """Store a payload body. Returns (key, sha256, byte_length)."""
    digest = sha256 or payload_sha256(payload)
    key = get_store().put(payload, digest)
    return key, digest, len(payload.encode("utf-8"))


def get(key: str) -> str:
    """Read a payload body back. Inverse of `put`."""
    return get_store().get(key)
