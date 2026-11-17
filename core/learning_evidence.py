"""Conservative source provenance and evidence-review helpers for ALE."""

from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source",
}


def normalize_source_url(value):
    """Normalize a public HTTPS URL for duplicate detection; reject unsafe forms."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
        ):
            return None
        port = parsed.port
    except (ValueError, UnicodeError):
        return None
    if port not in (None, 443):
        return None
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query_items = []
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered in _TRACKING_KEYS or any(
            lowered.startswith(prefix) for prefix in _TRACKING_PREFIXES
        ):
            continue
        query_items.append((key, item_value))
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit(("https", host, path, query, ""))


def assess_source_provenance(claim):
    """Describe URL/host diversity without claiming verified independence."""
    if not isinstance(claim, Mapping):
        claim = {}
    keys = claim.get("supporting_source_keys", [])
    if not isinstance(keys, (list, tuple, set)):
        keys = []
    knowledge_ids = claim.get("supporting_knowledge_ids", [])
    if not isinstance(knowledge_ids, (list, tuple, set)):
        knowledge_ids = []
    urls, hosts, unknown_records = set(), set(), set()
    for raw in keys:
        if not isinstance(raw, str):
            continue
        normalized = normalize_source_url(raw)
        if normalized:
            urls.add(normalized)
            hosts.add(urlsplit(normalized).hostname)
        elif raw.startswith("knowledge_record:"):
            unknown_records.add(raw.split(":", 1)[1])
        else:
            unknown_records.add(raw)
    if not keys:
        for record_id in knowledge_ids:
            if isinstance(record_id, str) and record_id:
                unknown_records.add(record_id)
    url_count, host_count, unknown_count = len(urls), len(hosts), len(unknown_records)
    if url_count == 0 and unknown_count == 0:
        state = "provenance_missing"
    elif url_count == 0:
        state = "record_identity_only"
    elif url_count == 1 and unknown_count:
        state = "single_url_with_unknown_records"
    elif url_count == 1:
        state = "single_url"
    elif host_count == 1 and unknown_count:
        state = "same_host_pages_with_unknown_records"
    elif host_count == 1:
        state = "multiple_pages_same_host"
    elif unknown_count:
        state = "multiple_hosts_with_unknown_records"
    else:
        state = "multiple_hosts_independence_unverified"
    return {
        "distinct_url_count": url_count,
        "distinct_host_count": host_count,
        "unknown_provenance_record_count": unknown_count,
        "source_diversity_state": state,
        "independent_source_count": None,
        "independence_verified": False,
        "truth_verified": False,
        "review_required": True,
        "disclaimer": (
            "Different URLs or hostnames do not establish independent ownership, "
            "editorial independence, source quality, or truth. Publisher relationships "
            "and the original evidence must be reviewed separately."
        ),
    }


def summarize_claim_provenance(claims):
    """Aggregate observable source diversity without inflating corroboration."""
    records = [assess_source_provenance(item) for item in claims]
    return {
        "claim_count": len(records),
        "single_url_claim_count": sum(item["distinct_url_count"] == 1 for item in records),
        "same_host_multi_url_claim_count": sum(
            item["source_diversity_state"] == "multiple_pages_same_host"
            for item in records
        ),
        "multi_host_candidate_claim_count": sum(
            item["distinct_host_count"] >= 2 for item in records
        ),
        "independently_corroborated_claim_count": 0,
        "independence_verification_available": False,
        "policy": (
            "URL and hostname diversity are candidate signals only. ALE does not "
            "currently verify publisher ownership, editorial independence, source "
            "quality, or factual truth."
        ),
    }


def _evidence_fields(record):
    """Read bounded metadata observations from the existing evidence ledger."""
    fields = {}
    entries = record.get("evidence", []) if isinstance(record, Mapping) else []
    if not isinstance(entries, list):
        return fields
    allowed = {
        "source_url", "page_author", "page_publisher", "page_published_at",
        "page_modified_at", "page_page_type", "page_canonical_url",
        "page_metadata_origin", "fetch_status",
    }
    for entry in entries:
        if not isinstance(entry, str) or ": " not in entry:
            continue
        key, value = entry.split(": ", 1)
        if key in allowed and value.strip() and key not in fields:
            fields[key] = value.strip()[:1000]
    return fields


def assess_claim_evidence(claim, source_records):
    """Build a review checklist; do not score source credibility or claim truth."""
    if not isinstance(claim, Mapping):
        claim = {}
    if not isinstance(source_records, (list, tuple)):
        source_records = []
    sources = []
    for record in source_records[:20]:
        if not isinstance(record, Mapping):
            continue
        observed = _evidence_fields(record)
        author = observed.get("page_author")
        publisher = observed.get("page_publisher")
        sources.append({
            "knowledge_id": record.get("id"),
            "url": observed.get("source_url"),
            "author": {"value": author, "state": "self_reported" if author else "unknown"},
            "publisher": {
                "value": publisher,
                "state": "self_reported" if publisher else "unknown",
            },
            "published_at": observed.get("page_published_at"),
            "modified_at": observed.get("page_modified_at"),
            "page_type": observed.get("page_page_type"),
            "canonical_url": observed.get("page_canonical_url"),
            "metadata_origin": observed.get("page_metadata_origin", "unknown"),
            "fetch_status": observed.get("fetch_status", "unknown"),
            "author_qualifications": "unknown",
            "publisher_ownership": "unverified",
            "syndication_or_source_dependency": "unknown",
        })
    return {
        "claim_id": claim.get("id"),
        "claim": claim.get("claim"),
        "assessment_state": "review_required",
        "claim_support_state": "not_evaluated",
        "source_reliability_state": "not_scored",
        "source_count": len(sources),
        "sources": sources,
        "review_questions": [
            "Does the cited passage directly support the exact claim?",
            "Does the evidence preserve the claim's conditions, scope, units, and dates?",
            "Is the cited material primary evidence or a secondary account?",
            "Are the author qualifications and publisher identity independently verifiable?",
            "Do the sources rely on the same press release, study, or upstream source?",
            "Is the evidence current for the claim, or has it been superseded?",
            "Is there credible evidence that conflicts with this claim?",
        ],
        "independence_verified": False,
        "truth_verified": False,
        "confidence_changed": False,
        "approval_state_changed": False,
        "disclaimer": (
            "HTML metadata is publisher-supplied and unverified. Missing fields remain "
            "unknown. This checklist does not evaluate entailment, source quality, "
            "independence, or factual truth and cannot approve or reject knowledge."
        ),
    }


__all__ = [
    "assess_claim_evidence",
    "assess_source_provenance",
    "normalize_source_url",
    "summarize_claim_provenance",
]
