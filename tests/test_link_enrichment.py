from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import link_enrichment
import memory_processor
from link_enrichment import LinkEnrichment, extract_urls, fetch_link


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise link_enrichment.requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 65536):
        del chunk_size
        yield self.body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs) -> FakeResponse:
        self.urls.append(url)
        return self.responses.pop(0)


def public_dns(*_args):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def test_extract_urls_trims_punctuation_and_deduplicates() -> None:
    text = "Read https://example.com/a?x=1, then https://example.com/a?x=1. And https://example.org/b)."

    assert extract_urls(text, 3) == ["https://example.com/a?x=1", "https://example.org/b"]


def test_private_destination_is_blocked_before_request(monkeypatch) -> None:
    monkeypatch.setattr(link_enrichment.socket, "getaddrinfo", lambda *_args: [(2, 1, 6, "", ("127.0.0.1", 0))])
    session = FakeSession([])

    result = fetch_link("http://internal.example/note", timeout_seconds=2, max_bytes=1000, session=session)

    assert result.status == "blocked"
    assert "private" in (result.reason or "")
    assert session.urls == []


def test_explicit_host_allowlist_permits_private_destination(monkeypatch) -> None:
    monkeypatch.setattr(link_enrichment.socket, "getaddrinfo", lambda *_args: [(2, 1, 6, "", ("192.168.1.20", 0))])
    response = FakeResponse(200, b"internal note text", {"Content-Type": "text/plain"})
    session = FakeSession([response])

    result = fetch_link(
        "http://notes.internal/page",
        timeout_seconds=2,
        max_bytes=1000,
        allowed_hosts=("notes.internal",),
        session=session,
    )

    assert result.status == "fetched"
    assert result.gist == "internal note text"


def test_redirect_destination_is_revalidated(monkeypatch) -> None:
    def dns(host, *_args):
        address = "127.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(2, 1, 6, "", (address, 0))]

    monkeypatch.setattr(link_enrichment.socket, "getaddrinfo", dns)
    redirect = FakeResponse(302, headers={"Location": "http://internal.example/private"})
    session = FakeSession([redirect])

    result = fetch_link("https://example.com/start", timeout_seconds=2, max_bytes=1000, session=session)

    assert result.status == "blocked"
    assert len(session.urls) == 1


def test_fetch_public_html_extracts_metadata_and_redacts_token(monkeypatch) -> None:
    monkeypatch.setattr(link_enrichment.socket, "getaddrinfo", public_dns)
    html = b"""
    <html><head><title>Useful Article</title>
    <meta name="description" content="A compact description of the article."></head>
    <body><script>ignore me</script><main><p>This is the useful factual body of the public article.</p></main></body></html>
    """
    response = FakeResponse(200, html, {"Content-Type": "text/html; charset=utf-8"})

    result = fetch_link(
        "https://example.com/article?token=sensitive&view=full",
        timeout_seconds=2,
        max_bytes=10000,
        session=FakeSession([response]),
    )

    assert result.status == "fetched"
    assert result.title == "Useful Article"
    assert "compact description" in (result.gist or "")
    assert "ignore me" not in (result.gist or "")
    assert "sensitive" not in result.url
    assert "token=%5Bredacted%5D" in result.url
    assert result.content_sha256


def test_save_memory_adds_untrusted_link_context_and_preserves_original_text(tmp_path: Path, monkeypatch) -> None:
    link = LinkEnrichment(
        url="https://example.com/article",
        final_url="https://example.com/article",
        status="fetched",
        title="Useful Article",
        description="Article description",
        gist="A short factual gist.",
        content_type="text/html",
        content_sha256="a" * 64,
        retrieved_at="2026-07-20T22:00:00+05:30",
    )
    monkeypatch.setattr(memory_processor, "enrich_text_links", lambda *_args: [link])
    captured: dict[str, str] = {}

    def fake_extract(raw_text, _cfg):
        captured["extraction_text"] = raw_text
        return {"title": "Saved link", "category": "note", "summary": "Summary", "key_fields": {}, "tags": []}

    monkeypatch.setattr(memory_processor, "extract_memory", fake_extract)
    cfg = SimpleNamespace(memory_dir=tmp_path / "memories", log_file=tmp_path / "worker.log")
    original = "Read this later: https://example.com/article"

    saved = memory_processor.save_memory(original, cfg, {"source": "test"})

    assert "UNTRUSTED LINKED CONTENT" in captured["extraction_text"]
    assert "A short factual gist." in captured["extraction_text"]
    assert "## Linked Content" in saved.content
    assert f"```text\n{original}\n```" in saved.content
