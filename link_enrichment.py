from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from image_summary import IST


URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}"
SENSITIVE_QUERY_KEYS = re.compile(r"(?:token|key|secret|password|passwd|auth|signature|sig|code)", re.IGNORECASE)
SUPPORTED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
SKIPPED_HTML_TAGS = {"script", "style", "noscript", "svg", "canvas", "template"}


class UnsafeUrlError(ValueError):
    pass


@dataclass(frozen=True)
class LinkEnrichment:
    url: str
    status: str
    final_url: str | None = None
    title: str | None = None
    description: str | None = None
    gist: str | None = None
    content_type: str | None = None
    content_sha256: str | None = None
    retrieved_at: str | None = None
    reason: str | None = None


def extract_urls(text: str, limit: int) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
        if url and url not in seen:
            urls.append(url)
            seen.add(url)
        if len(urls) >= max(0, limit):
            break
    return urls


def redact_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return "invalid-url"
    redacted_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        redacted_query.append((key, "[redacted]" if SENSITIVE_QUERY_KEYS.search(key) else value))
    host = parsed.hostname or ""
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, urlencode(redacted_query), ""))


def host_is_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    clean = host.rstrip(".").lower()
    for entry in allowed_hosts:
        allowed = entry.strip().rstrip(".").lower()
        if not allowed:
            continue
        if allowed.startswith("*.") and clean.endswith(allowed[1:]) and clean != allowed[2:]:
            return True
        if clean == allowed:
            return True
    return False


def validate_url(url: str, allowed_hosts: tuple[str, ...] = ()) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("only HTTP(S) URLs are supported")
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs containing credentials are blocked")
    host = parsed.hostname.rstrip(".").lower()
    explicitly_allowed = host_is_allowed(host, allowed_hosts)
    expected_port = 443 if parsed.scheme.lower() == "https" else 80
    if port not in (None, expected_port) and not explicitly_allowed:
        raise UnsafeUrlError("non-standard destination port is blocked")
    try:
        addresses = {item[4][0].split("%", 1)[0] for item in socket.getaddrinfo(host, port or expected_port)}
    except socket.gaierror as exc:
        raise UnsafeUrlError("hostname could not be resolved") from exc
    if not addresses:
        raise UnsafeUrlError("hostname resolved to no addresses")
    if not explicitly_allowed:
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise UnsafeUrlError("private or non-public destination is blocked")
    normalized_host = host.encode("idna").decode("ascii")
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


class ReadableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self._skip_depth = 0
        self._in_title = False
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in SKIPPED_HTML_TAGS:
            self._skip_depth += 1
        if lower == "title":
            self._in_title = True
        if lower == "meta":
            values = {str(key).lower(): str(value or "") for key, value in attrs}
            name = (values.get("name") or values.get("property") or "").lower()
            if name in {"description", "og:description", "twitter:description"} and not self.description:
                self.description = values.get("content", "").strip()

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in SKIPPED_HTML_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if lower == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        clean = re.sub(r"\s+", " ", unescape(data)).strip()
        if not clean or self._skip_depth:
            return
        if self._in_title:
            self.title = f"{self.title} {clean}".strip()
        else:
            self._text_parts.append(clean)

    def readable_text(self, max_chars: int = 12000) -> str:
        text = "\n".join(dict.fromkeys(self._text_parts))
        return text[:max_chars].strip()


def content_gist(title: str, description: str, text: str, max_chars: int = 1600) -> str:
    candidates = [description.strip()]
    candidates.extend(part.strip() for part in text.splitlines() if len(part.strip()) >= 40)
    gist = " ".join(dict.fromkeys(part for part in candidates if part))
    if not gist:
        gist = text.strip()
    if title and gist.lower().startswith(title.lower()):
        gist = gist[len(title) :].lstrip(" -:|")
    return gist[:max_chars].strip()


def fetch_link(
    url: str,
    *,
    timeout_seconds: int,
    max_bytes: int,
    allowed_hosts: tuple[str, ...] = (),
    session: Any | None = None,
) -> LinkEnrichment:
    display_url = redact_url(url)
    current = url
    client = session or requests.Session()
    if session is None:
        client.trust_env = False
    try:
        for _redirect in range(6):
            current = validate_url(current, allowed_hosts)
            response = client.get(
                current,
                allow_redirects=False,
                stream=True,
                timeout=max(1, timeout_seconds),
                headers={"User-Agent": "telegram-control-memory-link/1.0", "Accept": "text/html,text/plain;q=0.9"},
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise UnsafeUrlError("redirect had no destination")
                current = urljoin(current, location)
                continue
            if response.status_code >= 400:
                response.close()
                response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if not any(content_type == supported for supported in SUPPORTED_CONTENT_TYPES):
                response.close()
                raise UnsafeUrlError(f"unsupported content type: {content_type or 'unknown'}")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                response.close()
                raise UnsafeUrlError("response exceeds configured size limit")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > max_bytes:
                    response.close()
                    raise UnsafeUrlError("response exceeds configured size limit")
            response.close()
            encoding = response.encoding or "utf-8"
            decoded = bytes(body).decode(encoding, errors="replace")
            title = description = ""
            if content_type in {"text/html", "application/xhtml+xml"}:
                parser = ReadableHtmlParser()
                parser.feed(decoded)
                title = parser.title[:300].strip()
                description = parser.description[:600].strip()
                readable = parser.readable_text()
            else:
                readable = decoded[:12000].strip()
            gist = content_gist(title, description, readable)
            return LinkEnrichment(
                url=display_url,
                status="fetched",
                final_url=redact_url(current),
                title=title or None,
                description=description or None,
                gist=gist or None,
                content_type=content_type,
                content_sha256=hashlib.sha256(bytes(body)).hexdigest(),
                retrieved_at=dt.datetime.now(IST).isoformat(timespec="seconds"),
            )
        raise UnsafeUrlError("too many redirects")
    except UnsafeUrlError as exc:
        return LinkEnrichment(url=display_url, status="blocked", final_url=redact_url(current), reason=str(exc))
    except requests.RequestException as exc:
        return LinkEnrichment(url=display_url, status="failed", final_url=redact_url(current), reason=type(exc).__name__)
    except (OSError, UnicodeError, ValueError) as exc:
        return LinkEnrichment(url=display_url, status="failed", final_url=redact_url(current), reason=type(exc).__name__)


def enrich_text_links(text: str, cfg: Any) -> list[LinkEnrichment]:
    if not getattr(cfg, "memory_link_enrichment_enabled", True):
        return []
    urls = extract_urls(text, int(getattr(cfg, "memory_link_max_urls", 3)))
    return [
        fetch_link(
            url,
            timeout_seconds=int(getattr(cfg, "memory_link_timeout_seconds", 10)),
            max_bytes=int(getattr(cfg, "memory_link_max_bytes", 2 * 1024 * 1024)),
            allowed_hosts=tuple(getattr(cfg, "memory_link_allowed_hosts", ())),
        )
        for url in urls
    ]
