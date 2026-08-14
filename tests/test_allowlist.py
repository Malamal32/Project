import pytest

from pipeline.allowlist import NotAllowlistedError, is_allowlisted, load_allowlist, require_allowlisted


def test_shipped_allowlist_is_empty():
    assert load_allowlist() == set()


def test_unlisted_domain_is_not_allowlisted():
    assert not is_allowlisted("example.com")


def test_require_allowlisted_raises_for_unlisted_domain():
    with pytest.raises(NotAllowlistedError):
        require_allowlisted("example.com")


def test_custom_allowlist_file(tmp_path):
    path = tmp_path / "allowlist.csv"
    path.write_text("domain,company_name,added_at,notes\nexample.com,Example Co,2026-01-01,test\n")

    assert is_allowlisted("example.com", path=path)
    assert is_allowlisted("EXAMPLE.COM", path=path)
    require_allowlisted("example.com", path=path)  # must not raise
