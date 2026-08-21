"""Coverage for the content-addressed raw payload store.

The store is what makes `raw_documents` immutability hold once bodies live outside
the database: the key is derived from the content, so the same bytes always land in
the same place and different bytes can never overwrite each other.
"""

from __future__ import annotations

from pipeline import raw_store


def test_key_is_derived_from_content(tmp_path):
    store = raw_store.LocalStore(tmp_path)
    digest = raw_store.payload_sha256("hello")

    assert store.put("hello", digest) == raw_store.key_for(digest)
    assert raw_store.key_for(digest).endswith(digest)


def test_round_trips_utf8(tmp_path):
    store = raw_store.LocalStore(tmp_path)
    payload = 'Ingeniería de software — "señor" developer ☕'
    key = store.put(payload, raw_store.payload_sha256(payload))

    assert store.get(key) == payload


def test_identical_payloads_share_one_key(tmp_path):
    store = raw_store.LocalStore(tmp_path)
    digest = raw_store.payload_sha256("same bytes")

    first = store.put("same bytes", digest)
    second = store.put("same bytes", digest)

    assert first == second
    assert len(list(tmp_path.rglob("*"))) == 3  # raw/ + raw/xx/ + the one object


def test_different_payloads_get_different_keys(tmp_path):
    store = raw_store.LocalStore(tmp_path)

    a = store.put("alpha", raw_store.payload_sha256("alpha"))
    b = store.put("beta", raw_store.payload_sha256("beta"))

    assert a != b
    assert store.get(a) == "alpha"
    assert store.get(b) == "beta"


def test_put_reports_key_digest_and_byte_length():
    # The autouse `local_raw_store` fixture already points the module-level store at
    # a temp directory, so this exercises the real `put` path.
    payload = "café"  # 5 bytes in UTF-8, 4 characters

    key, digest, size = raw_store.put(payload)

    assert key == raw_store.key_for(digest)
    assert digest == raw_store.payload_sha256(payload)
    assert size == 5


def test_get_store_falls_back_to_local_without_r2_credentials(monkeypatch):
    """The fallback is what keeps every stage runnable, and the suite offline,
    without an R2 account."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.delenv("R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("R2_SECRET_ACCESS_KEY", raising=False)
    raw_store.get_store.cache_clear()

    try:
        assert isinstance(raw_store.get_store(), raw_store.LocalStore)
    finally:
        raw_store.get_store.cache_clear()
