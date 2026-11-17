"""Bounded live-web capability for Amiki.

Search results are treated as untrusted external data. This module is read-only,
uses short network timeouts, and never executes page content.
"""

from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse
import ipaddress
import re
import socket

import requests

from core.web_evidence import build_evidence, evidence_for_prompt


_TIMEOUT = (3.0, 8.0)
_MAX_RESULTS = 5
_MAX_SNIPPET = 500
_MAX_QUERY = 300
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_USER_AGENT = "Amiki/0.1 live-web capability"
_ALLOWED_HOSTS = {"html.duckduckgo.com", "lite.duckduckgo.com", "geocoding-api.open-meteo.com", "api.open-meteo.com"}
_SEARCH_ENDPOINTS = ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/")
_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".home", ".lan")
_WEATHER_LOCATION_RE = re.compile(r"\b(?:in|for|at|near)\s+([A-Za-z][A-Za-z .\'\-]{1,80}?)(?:\s+(?:right\s+now|now|today|tonight|currently))?[?!.]*$", re.I)

_BLOCKED_IP_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")


class _DuckParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._title = []
        self._snippet = []
        self._href = None
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())

        if tag == "a" and "result-link" in classes:
            self._href = attrs.get("href")
            self._title = []
            self._in_title = True
        elif tag in {"a", "div"} and (
            "result-snippet" in classes or "result__snippet" in classes
        ):
            self._snippet = []
            self._in_snippet = True

    def handle_endtag(self, tag):
        if tag == "a" and self._in_title:
            title = "".join(self._title).strip()
            if title and self._href:
                self.results.append({
                    "title": title,
                    "url": self._normalise_url(self._href),
                    "snippet": "",
                })
            self._title = []
            self._href = None
            self._in_title = False
        elif tag in {"a", "div"} and self._in_snippet:
            snippet = "".join(self._snippet).strip()
            if snippet and self.results:
                self.results[-1]["snippet"] = snippet[:_MAX_SNIPPET]
            self._snippet = []
            self._in_snippet = False

    def handle_data(self, data):
        if self._in_title:
            self._title.append(data)
        if self._in_snippet:
            self._snippet.append(data)

    @staticmethod
    def _normalise_url(value):
        parsed = urlparse(value)
        if parsed.netloc.endswith("duckduckgo.com"):
            target = parse_qs(parsed.query).get("uddg", [None])[0]
            if target:
                return unquote(target)
        return value


def _bounded_response_text(response):
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536, decode_unicode=True):
        if not chunk:
            continue
        total += len(chunk.encode("utf-8", errors="ignore"))
        if total > _MAX_RESPONSE_BYTES:
            raise ValueError("web response exceeded safety limit")
        chunks.append(chunk)
    return "".join(chunks)


def _safe_external_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False

    host = parsed.hostname.lower().rstrip(".")
    if host in _ALLOWED_HOSTS:
        return True
    if host.endswith(_BLOCKED_HOST_SUFFIXES) or host in {"localhost", "0.0.0.0"}:
        return False

    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        return False

    if not addresses:
        return False

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
            or ip.is_multicast
        ):
            return False

    return True


def _search(query):
    query = " ".join(str(query).split())[:_MAX_QUERY]
    if not query:
        return []

    last_error = None
    for endpoint in _SEARCH_ENDPOINTS:
        try:
            response = requests.get(
                endpoint,
                params={"q": query},
                headers={"User-Agent": _USER_AGENT},
                timeout=_TIMEOUT,
                stream=True,
                allow_redirects=False,
            )
            try:
                if 300 <= response.status_code < 400:
                    raise ValueError("web provider redirect rejected")
                response.raise_for_status()

                parser = _DuckParser()
                parser.feed(_bounded_response_text(response))

                results = []
                for item in parser.results:
                    url = item.get("url", "")
                    if not _safe_external_url(url):
                        continue
                    item["title"] = " ".join(item["title"].split())[:200]
                    item["snippet"] = " ".join(item["snippet"].split())[:_MAX_SNIPPET]
                    results.append(item)
                    if len(results) >= _MAX_RESULTS:
                        break

                # A successful HTTP response with no parseable results is not
                # evidence of "no results": the provider format may have changed.
                if results:
                    return results
                last_error = ValueError("web provider returned no parseable results")
            finally:
                response.close()
        except Exception as exc:
            last_error = exc

    if last_error:
        raise last_error
    return []


def _extract_weather_location(message):
    text = " ".join(str(message).split()).strip()
    match = _WEATHER_LOCATION_RE.search(text)
    if match:
        return match.group(1).strip(" ,.?!")
    return text[:100]


def _weather(query):
    # Search results are never fetched here. Only explicitly allowlisted APIs are called.
    geocode = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": query, "count": 1, "language": "en", "format": "json"},
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
        allow_redirects=False,
    )
    if 300 <= geocode.status_code < 400:
        raise ValueError("geocoding redirect rejected")
    geocode.raise_for_status()
    places = geocode.json().get("results") or []
    if not places:
        return None

    place = places[0]
    forecast = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "weather_code,wind_speed_10m",
            "timezone": "auto",
        },
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
        allow_redirects=False,
    )
    if 300 <= forecast.status_code < 400:
        raise ValueError("forecast redirect rejected")
    forecast.raise_for_status()
    forecast_data = forecast.json()
    current = forecast_data.get("current") or {}

    return {
        "location": place.get("name", query),
        "country": place.get("country", ""),
        "observed_at": current.get("time"),
        "timezone": forecast_data.get("timezone"),
        "temperature_c": current.get("temperature_2m"),
        "apparent_temperature_c": current.get("apparent_temperature"),
        "humidity_percent": current.get("relative_humidity_2m"),
        "wind_kmh": current.get("wind_speed_10m"),
        "weather_code": current.get("weather_code"),
        "source": "Open-Meteo",
    }


def _is_weather_query(message):
    lowered = message.lower()
    return "weather" in lowered or "temperature" in lowered


def handle(message):
    """Perform a bounded live lookup and return facts for the model."""
    try:
        if _is_weather_query(message):
            weather = _weather(_extract_weather_location(message))
            if weather:
                evidence = build_evidence(
                    provider="Open-Meteo",
                    query=message,
                    facts=weather,
                )
                return {
                    "action": "live_weather",
                    "evidence": evidence_for_prompt(evidence),
                }

        results = _search(message)
        evidence = build_evidence(
            provider="DuckDuckGo",
            query=message,
            sources=results,
            snippets=[item.get("snippet", "") for item in results],
        )
        return {
            "action": "live_web_search",
            "evidence": evidence_for_prompt(evidence),
        }
    except Exception:
        return {
            "action": "live_web_unavailable",
            "status": "unavailable",
            "message": (
                "Live web lookup is temporarily unavailable. "
                "Answer only from existing knowledge and clearly say that "
                "live verification was unavailable."
            ),
        }
