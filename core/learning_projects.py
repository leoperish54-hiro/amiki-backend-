"""Subject-directed learning project planning and bounded research orchestration."""
import re


_LEARNING_REQUEST_PATTERNS = (
    re.compile(r"^(?:i want to learn|i would like to learn|help me learn|teach me|learn about|study)\s+(.+)$", re.I),
    re.compile(r"^(?:can you|could you|please)\s+(?:teach me|help me learn)\s+(.+)$", re.I),
    re.compile(r"^(?:i want you to learn|i want amiki to learn)\s+(.+)$", re.I),
)
_TRAILING_CONTEXT = re.compile(
    r"\s+(?:from scratch|as a beginner|for beginners|for my exam|for university)$",
    re.I,
)
_MAX_SUBJECT = 180
_MAX_QUERY = 300
_MAX_RESEARCH_QUERIES = 3
_MAX_RESEARCH_SOURCES = 3


def detect_learning_request(message):
    """Return a bounded subject only for an explicit learn/study instruction."""
    if not isinstance(message, str):
        return None
    text = " ".join(message.strip().split()).rstrip(" .!?")
    if not text or len(text) > 500:
        return None
    for pattern in _LEARNING_REQUEST_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        subject = _TRAILING_CONTEXT.sub("", match.group(1).strip()).strip(" \t.,!?;:")
        if len(subject) < 2 or len(subject) > _MAX_SUBJECT:
            return None
        if subject.casefold() in {"me", "something", "anything", "everything", "a subject"}:
            return None
        return subject
    return None


def build_research_queries(subject):
    """Create a small, subject-independent and bounded research plan."""
    if not isinstance(subject, str) or not subject.strip() or len(subject) > _MAX_SUBJECT:
        raise ValueError("subject must be a non-empty string of at most 180 characters")
    normalized = " ".join(subject.split())
    templates = (
        f"{normalized} fundamentals core concepts introductory learning objectives",
        f"{normalized} authoritative documentation university course textbook",
        f"{normalized} practical examples applications exercises",
    )
    return [query[:_MAX_QUERY] for query in templates[:_MAX_RESEARCH_QUERIES]]


def learning_stages(subject):
    """Return a domain-neutral scaffold; a tutor must tailor lesson details."""
    return [
        {"id": "orientation", "title": f"Understand {subject}", "purpose": "Define the field, scope, vocabulary, and baseline knowledge."},
        {"id": "foundations", "title": "Build the foundations", "purpose": "Identify prerequisites and learn essential concepts in dependency order."},
        {"id": "core-concepts", "title": "Study the core concepts", "purpose": "Connect concepts, models, principles, and their evidence."},
        {"id": "applications", "title": "Apply what you learn", "purpose": "Work through examples, practical tasks, and real applications."},
        {"id": "assessment", "title": "Test understanding and transfer", "purpose": "Use retrieval practice, unfamiliar problems, and feedback to identify gaps."},
    ]


def run_learning_request(subject):
    """Create a subject project and collect up to three distinct public source pages."""
    from core.learning_engine import (
        create_learning_project,
        record_learning_project_research,
    )
    from core.handlers.web import _search
    from core.research_learning import capture_research_candidates
    from core.web_evidence import build_evidence, evidence_for_prompt

    project = create_learning_project(subject)
    query_results = []
    knowledge_ids = []
    seen_urls = set()
    source_total = 0

    for planned in project["research_queries"]:
        query = planned["query"]
        if source_total >= _MAX_RESEARCH_SOURCES:
            query_results.append({
                "query": query, "status": "empty", "source_count": 0,
            })
            continue

        try:
            results = _search(query)
        except Exception:
            query_results.append({
                "query": query, "status": "unavailable", "source_count": 0,
            })
            continue

        selected = []
        for item in results:
            url = item.get("url") if isinstance(item, dict) else None
            if not isinstance(url, str) or not url or url in seen_urls:
                continue
            seen_urls.add(url)
            selected.append(item)
            if len(selected) >= 1 or source_total + len(selected) >= _MAX_RESEARCH_SOURCES:
                break

        if not selected:
            query_results.append({
                "query": query, "status": "empty", "source_count": 0,
            })
            continue

        evidence = evidence_for_prompt(build_evidence(
            provider="DuckDuckGo",
            query=query,
            sources=[
                {"title": item.get("title", ""), "url": item.get("url", "")}
                for item in selected
            ],
            snippets=[item.get("snippet", "") for item in selected],
        ))
        dispatch = {
            "capability": "web",
            "status": "handled",
            "result": {
                "action": "live_web_search",
                "status": "success",
                "evidence": evidence,
            },
        }
        try:
            new_ids = capture_research_candidates(dispatch)
        except Exception:
            # Search results remain visible as a research failure; no unreviewed
            # text is copied directly into the project store.
            new_ids = []
        for record_id in new_ids:
            if record_id not in knowledge_ids:
                knowledge_ids.append(record_id)
        source_total += len(selected)
        query_results.append({
            "query": query,
            "status": "success",
            "source_count": len(selected),
        })

    updated = record_learning_project_research(
        project["id"],
        query_results=query_results,
        knowledge_ids=knowledge_ids,
    )
    return {
        "project_id": updated["id"],
        "subject": updated["subject"],
        "goal": updated["goal"],
        "project_status": updated["status"],
        "stages": updated["stages"],
        "research": {
            "queries": updated["research_queries"],
            "source_pages_found": source_total,
            "knowledge_candidates_created": len(updated["knowledge_ids"]),
            "extracted_content_pages": len(updated.get("extracted_knowledge_ids", [])),
            "reference_only_pages": (
                len(updated["knowledge_ids"])
                - len(updated.get("extracted_knowledge_ids", []))
            ),
            "knowledge_ids": updated["knowledge_ids"],
            "review_required": True,
        },
        "limitations": [
            "Fetched source records are untrusted and remain pending owner review.",
            "Search coverage is a bounded sample, not an exhaustive literature review.",
            "The stages are a general scaffold; the tutor must tailor lesson content to the subject.",
        ],
    }



__all__ = ["build_research_queries", "detect_learning_request", "learning_stages", "run_learning_request"]
