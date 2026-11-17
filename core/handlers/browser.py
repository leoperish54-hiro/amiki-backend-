"""Bounded read-only browser fetcher for Amiki.

Browser V1 performs only explicit HTTPS GET navigation. It does not execute
JavaScript, submit forms, persist cookies, follow unvalidated redirects, or
grant the model arbitrary network access.
"""

from html.parser import HTMLParser
from html import unescape
from urllib.parse import urljoin, urlparse

import requests

from core.handlers.web import _safe_external_url
from core.web_evidence import build_evidence, evidence_for_prompt


_TIMEOUT = (3.0, 8.0)
_MAX_REDIRECTS = 3
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_URL_LENGTH = 2048
_MAX_LINK_CANDIDATES = 40
_MAX_PAGE_TEXT = 12000
_MAX_LINKS = 10
_MAX_LINK_TITLE = 160
_MAX_METADATA_VALUE = 300
_ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_METADATA_KEYS = {
    "author": "author",
    "article:author": "author",
    "citation_author": "author",
    "publisher": "publisher",
    "og:site_name": "publisher",
    "citation_publication_title": "publisher",
    "article:published_time": "published_at",
    "datepublished": "published_at",
    "citation_publication_date": "published_at",
    "article:modified_time": "modified_at",
    "datemodified": "modified_at",
    "og:type": "page_type",
}


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.text_parts = []
        self.links = []
        self.metadata = {}
        self.canonical_href = None
        self._in_title = False
        self._current_link = None
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)

        if tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").strip().casefold()
            field = _METADATA_KEYS.get(key)
            value = " ".join(str(attrs.get("content") or "").split())
            if field and value and field not in self.metadata:
                self.metadata[field] = value[:_MAX_METADATA_VALUE]
            return

        if tag == "link" and "canonical" in (attrs.get("rel") or "").casefold().split():
            href = attrs.get("href")
            if isinstance(href, str) and href.strip() and self.canonical_href is None:
                self.canonical_href = href.strip()[:_MAX_URL_LENGTH]
            return

        if tag == "title":
            self._in_title = True
            return

        if tag in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
            return

        if tag == "a" and self._skip_depth == 0 and len(self.links) < _MAX_LINK_CANDIDATES:
            href = attrs.get("href")
            if href:
                self.links.append({"href": href, "title": ""})
                self._current_link = len(self.links) - 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            return
        if tag in {"script", "style", "noscript", "template"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "a":
            self._current_link = None

    def handle_data(self, data):
        if self._skip_depth:
            return
        clean = " ".join(data.split())
        if not clean:
            return
        if self._in_title:
            self.title_parts.append(clean)
        else:
            self.text_parts.append(clean)
        index = getattr(self, "_current_link", None)
        if index is not None and index < len(self.links):
            current = self.links[index]["title"]
            self.links[index]["title"] = (current + " " + clean).strip()[:_MAX_LINK_TITLE]


def _bounded_response_text(response):
    declared_length = response.headers.get("Content-Length")
    if declared_length:
        try:
            if int(declared_length) > _MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded browser safety limit")
        except (TypeError, ValueError):
            if str(declared_length).strip() != "0":
                raise ValueError("invalid response length")
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536, decode_unicode=True):
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            total += len(chunk)
            piece = chunk.decode(response.encoding or "utf-8", errors="replace")
        else:
            piece = str(chunk)
            total += len(piece.encode("utf-8", errors="ignore"))
        if total > _MAX_RESPONSE_BYTES:
            raise ValueError("response exceeded browser safety limit")
        chunks.append(piece)
    return "".join(chunks)


def _normalise_target(url, base=None):
    target = urljoin(base, url) if base else str(url or "").strip()
    if not target or len(target) > _MAX_URL_LENGTH or any(ord(ch) < 32 for ch in target):
        return None
    parsed = urlparse(target)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        return None
    if not _safe_external_url(target):
        return None
    return target.split("#", 1)[0]


def _extract_links(base_url, links):
    safe = []
    seen = set()
    for item in links:
        target = _normalise_target(item.get("href"), base=base_url)
        if not target or target in seen:
            continue
        seen.add(target)
        title = " ".join(str(item.get("title") or "").split())[:_MAX_LINK_TITLE]
        safe.append({"title": title, "url": target})
        if len(safe) >= _MAX_LINKS:
            break
    return safe


def _fetch_page(url):
    current = _normalise_target(url)
    if not current:
        raise ValueError("browser target rejected")
    for _ in range(_MAX_REDIRECTS + 1):
        response = requests.get(
            current,
            headers={"User-Agent": "Amiki/0.1 browser-v1"},
            timeout=_TIMEOUT,
            stream=True,
            allow_redirects=False,
        )
        try:
            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                current = _normalise_target(location, base=current)
                if not current:
                    raise ValueError("browser redirect rejected")
                continue
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in _ALLOWED_CONTENT_TYPES:
                raise ValueError("browser content type rejected")
            return current, _bounded_response_text(response)
        finally:
            response.close()
    raise ValueError("browser redirect limit exceeded")


def _chunk_text(text):
    text = " ".join(text.split())[:_MAX_PAGE_TEXT]
    return [text[index:index + 500] for index in range(0, len(text), 500) if text[index:index + 500]]


def _page_metadata(parser, final_url):
    """Return explicitly declared HTML metadata; never infer missing identity or trust."""
    metadata = dict(parser.metadata)
    if parser.canonical_href:
        canonical = _normalise_target(parser.canonical_href, base=final_url)
        if canonical:
            metadata["canonical_url"] = canonical
    metadata["metadata_origin"] = "html_meta"
    return metadata


def extract_page(url):
    """Extract bounded page text and explicitly declared, unverified HTML metadata."""
    final_url, html = _fetch_page(url)
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    title = unescape(" ".join(parser.title_parts).strip())[:_MAX_LINK_TITLE]
    page_text = " ".join(parser.text_parts)
    return {
        "url": final_url,
        "title": title or "(untitled page)",
        "text": " ".join(page_text.split())[:_MAX_PAGE_TEXT],
        "metadata": _page_metadata(parser, final_url),
    }


def browse(url):
    """Fetch and safely project one public page into untrusted evidence."""
    try:
        final_url, html = _fetch_page(url)
        parser = _PageParser()
        parser.feed(html)
        title = unescape(" ".join(parser.title_parts).strip())[:_MAX_LINK_TITLE]
        page_text = " ".join(parser.text_parts)
        links = _extract_links(final_url, parser.links)
        facts = {"title": title or "(untitled page)"}
        metadata = _page_metadata(parser, final_url)
        evidence = build_evidence(
            provider="Amiki Browser V1",
            query=final_url,
            facts=facts,
            sources=[{"title": item["title"] or item["url"], "url": item["url"]} for item in links],
            snippets=_chunk_text(page_text),
        )
        projected = evidence_for_prompt(evidence)
        projected["page_metadata_observations"] = metadata
        return {"action": "live_web_page", "evidence": projected}
    except Exception:
        return {
            "action": "live_web_page_unavailable",
            "status": "unavailable",
            "message": (
                "The requested web page could not be safely retrieved. "
                "No page content is available."
            ),
        }


__all__ = ["browse", "extract_page"]
