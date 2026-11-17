"""Convert successful web-search evidence into review-only ALE candidates.

Only source-backed search results are eligible. Ephemeral weather observations,
failed lookups, and snippets flagged by the evidence firewall are excluded.
External text remains untrusted and is never promoted automatically.
"""

from collections.abc import Mapping

from core.learning_engine import propose_knowledge


_MAX_CANDIDATES = 5
_TRUST_BOUNDARY = "UNTRUSTED_EXTERNAL_EVIDENCE"
_PAGE_METADATA_FIELDS = (
    "author", "publisher", "published_at", "modified_at",
    "page_type", "canonical_url", "metadata_origin",
)


def _research_payloads(dispatch):
    if not isinstance(dispatch, Mapping):
        return []
    if dispatch.get("capability") == "web":
        payload = dispatch.get("result")
        return [payload] if isinstance(payload, Mapping) else []
    if dispatch.get("capability") != "agent":
        return []
    result = dispatch.get("result")
    if not isinstance(result, Mapping) or result.get("agent") != "research":
        return []
    payloads = []
    for item in result.get("results", []):
        if not isinstance(item, Mapping):
            continue
        payload = item.get("data")
        for _ in range(2):
            if not isinstance(payload, Mapping):
                break
            if payload.get("action") in {
                "live_web_search", "live_weather", "live_web_unavailable"
            }:
                break
            nested = payload.get("data")
            if not isinstance(nested, Mapping):
                break
            payload = nested
        if isinstance(payload, Mapping):
            payloads.append(payload)
    return payloads


def _metadata_evidence(metadata):
    """Serialize only allowlisted, explicitly observed HTML metadata fields."""
    if not isinstance(metadata, Mapping):
        return []
    result = []
    for field in _PAGE_METADATA_FIELDS:
        value = metadata.get(field)
        if isinstance(value, str) and value.strip():
            clean = " ".join(value.split())[:300]
            result.append(f"page_{field}: {clean}")
    return result


def capture_research_candidates(dispatch):
    """Create review-only candidates from individually extracted source pages."""
    from core.handlers import browser
    from core.web_evidence import contains_instruction_pattern

    created = []
    for payload in _research_payloads(dispatch):
        if payload.get("action") != "live_web_search":
            continue
        if payload.get("status") not in (None, "success"):
            continue
        evidence = payload.get("evidence")
        if not isinstance(evidence, Mapping) or evidence.get("trust_boundary") != _TRUST_BOUNDARY:
            continue
        provider = evidence.get("provider")
        query = evidence.get("query")
        sources = evidence.get("sources")
        snippets = evidence.get("untrusted_content", [])
        if (
            not isinstance(provider, str) or not provider.strip()
            or not isinstance(query, str) or not query.strip()
            or not isinstance(sources, list)
            or not isinstance(snippets, list)
        ):
            continue
        retrieved_at = evidence.get("retrieved_at")

        for index, source in enumerate(sources[:_MAX_CANDIDATES]):
            if not isinstance(source, Mapping):
                continue
            title = source.get("title")
            url = source.get("url")
            if (
                not isinstance(title, str) or not title.strip()
                or "potential prompt-injection content removed" in title.lower()
            ):
                continue
            if index < len(snippets):
                snippet_record = snippets[index]
                if not isinstance(snippet_record, Mapping):
                    continue
                if snippet_record.get("contains_instruction_pattern") is True:
                    continue
            try:
                safe_url = browser._normalise_target(url)
            except (TypeError, ValueError):
                continue
            if not safe_url:
                continue

            page = None
            try:
                page = browser.extract_page(safe_url)
            except Exception:
                pass

            final_url = safe_url
            page_title = title.strip()[:200]
            page_text = ""
            page_metadata = {}
            exclusion_reason = None
            if isinstance(page, Mapping):
                try:
                    extracted_url = browser._normalise_target(page.get("url"))
                except (TypeError, ValueError):
                    extracted_url = None
                extracted_text = page.get("text")
                extracted_title = page.get("title")
                metadata = page.get("metadata")
                if isinstance(metadata, Mapping):
                    page_metadata = {
                        field: value for field, value in metadata.items()
                        if field in _PAGE_METADATA_FIELDS
                        and isinstance(value, str) and value.strip()
                    }
                if not extracted_url:
                    exclusion_reason = "invalid_final_url"
                else:
                    final_url = extracted_url
                    if isinstance(extracted_title, str) and extracted_title.strip():
                        if contains_instruction_pattern(extracted_title):
                            exclusion_reason = "instruction_pattern_detected"
                        else:
                            page_title = extracted_title.strip()[:200]
                    if isinstance(extracted_text, str):
                        candidate_text = " ".join(extracted_text.split())[:12000]
                        if contains_instruction_pattern(candidate_text):
                            exclusion_reason = "instruction_pattern_detected"
                        elif not exclusion_reason:
                            page_text = candidate_text

            if page_text and not exclusion_reason:
                content = (
                    f"{page_title}\n\n"
                    "Extracted page content (untrusted): "
                    f"{page_text}"
                )
                fetch_status = "extracted"
            else:
                content = (
                    f"{title.strip()[:200]}\n\n"
                    "Reference only: extracted content was unavailable or excluded "
                    "by the safety filter. No page text is stored."
                )
                fetch_status = "reference_only"

            provenance = [
                f"provider: {provider[:100]}",
                f"query: {query[:300]}",
                f"source_url: {final_url[:1000]}",
                f"requested_source_url: {safe_url[:1000]}",
                f"fetch_status: {fetch_status}",
            ]
            provenance.extend(_metadata_evidence(page_metadata))
            if exclusion_reason:
                provenance.append(f"content_exclusion_reason: {exclusion_reason}")
            if isinstance(retrieved_at, str) and retrieved_at.strip():
                provenance.append(f"retrieved_at: {retrieved_at[:80]}")

            record_id = propose_knowledge(
                topic=query.strip()[:200],
                content=content[:16000],
                source=f"{provider[:100]} research result",
                evidence=provenance,
                confidence=0.35,
            )
            created.append(record_id)
    return created


__all__ = ["capture_research_candidates"]
