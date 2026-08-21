import json
from datetime import datetime, timezone

from pipeline.collectors.base import PostingStub, RawDocument
from pipeline.collectors.greenhouse import GreenhouseCollector, board_token_from_base_url, strip_html


class FakeGate:
    """Stands in for PolitenessGate: no network, no robots.txt, no rate limiting."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url, politeness_config=None):
        self.calls.append(url)
        payload = self.responses[url]

        class _Resp:
            status_code = 200
            headers = {"content-type": "application/json"}
            text = json.dumps(payload)

            def json(self_inner):
                return payload

        return _Resp()


def test_board_token_from_base_url():
    assert board_token_from_base_url("https://boards-api.greenhouse.io/v1/boards/stripe") == "stripe"
    assert board_token_from_base_url("https://boards-api.greenhouse.io/v1/boards/stripe/") == "stripe"


def test_strip_html_unescapes_entities_before_stripping_tags():
    # Greenhouse's content field is HTML-entity-escaped, not raw HTML.
    raw = "&lt;h2&gt;&lt;strong&gt;Hello&lt;/strong&gt; world&lt;/h2&gt;"
    assert strip_html(raw) == "Hello world"


def test_discover_yields_stubs_from_listing():
    base_url = "https://boards-api.greenhouse.io/v1/boards/acme"
    gate = FakeGate({
        f"{base_url}/jobs?content=false": {"jobs": [{"id": 1, "absolute_url": "https://acme.com/jobs/1"}]}
    })
    collector = GreenhouseCollector(source_id="src", base_url=base_url, politeness_config={}, gate=gate)

    stubs = list(collector.discover())

    assert stubs == [PostingStub(source_posting_id="1", url="https://acme.com/jobs/1")]


def test_fetch_and_parse_round_trip():
    base_url = "https://boards-api.greenhouse.io/v1/boards/acme"
    job = {
        "id": 42,
        "title": "Engineer",
        "company_name": "Acme",
        "content": "&lt;p&gt;Build things&lt;/p&gt;",
        "location": {"name": "Austin, TX"},
        "first_published": "2026-01-01T00:00:00-05:00",
        "absolute_url": "https://acme.com/jobs/42",
    }
    gate = FakeGate({f"{base_url}/jobs/42": job})
    collector = GreenhouseCollector(source_id="src", base_url=base_url, politeness_config={}, gate=gate)

    raw = collector.fetch(PostingStub(source_posting_id="42", url="https://acme.com/jobs/42"))
    record = collector.parse(raw)

    assert record.source_posting_id == "42"
    assert record.title_raw == "Engineer"
    assert record.description_text == "Build things"
    assert record.location_raw == "Austin, TX"
    assert record.company_name_raw == "Acme"


def test_parse_returns_none_on_invalid_json():
    collector = GreenhouseCollector(
        source_id="src", base_url="https://boards-api.greenhouse.io/v1/boards/acme",
        politeness_config={}, gate=None,
    )
    raw = RawDocument(
        url="x", fetched_at=datetime.now(timezone.utc), http_status=200,
        content_type="application/json", payload="not json",
    )
    assert collector.parse(raw) is None


def test_parse_returns_none_when_required_fields_missing():
    collector = GreenhouseCollector(
        source_id="src", base_url="https://boards-api.greenhouse.io/v1/boards/acme",
        politeness_config={}, gate=None,
    )
    raw = RawDocument(
        url="x", fetched_at=datetime.now(timezone.utc), http_status=200,
        content_type="application/json", payload=json.dumps({"id": None, "title": None}),
    )
    assert collector.parse(raw) is None
