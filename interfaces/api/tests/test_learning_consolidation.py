"""Review-first ALE consolidation tests inspired by episodic memory architectures."""

import pytest

from core import learning_engine as learning
from core import uosa_security
from interfaces.api.server.app import app
from interfaces.api.tests.uosa_test_client import UosaTestClient

OWNER_TOKEN = "test-owner-learning-token-that-is-at-least-32-chars"
client = UosaTestClient(app, OWNER_TOKEN)


@pytest.fixture
def learning_store(tmp_path, monkeypatch):
    store = tmp_path / "learning.json"
    monkeypatch.setattr(learning, "STORE_FILE", str(store))
    monkeypatch.setattr(learning, "LEARNING_ENABLED", True)
    return store


def _headers():
    return {"Authorization": f"Bearer {OWNER_TOKEN}"}


def test_submit_learning_note_preserves_full_content_and_is_idempotent(learning_store):
    content = (
        "A Python generator yields values lazily. It suspends execution at each "
        "yield expression and resumes when the next value is requested."
    )
    first = learning.submit_learning_note("Python generators", content)
    duplicate = learning.submit_learning_note("Python generators", content)
    different_topic = learning.submit_learning_note("Python iterators", content)

    assert first == duplicate
    assert different_topic != first

    stored = learning.get_review_item(first)
    assert stored["kind"] == "owner_learning_note"
    assert stored["content"] == content
    assert stored["topic"] == "Python generators"
    assert stored["status"] == "recorded"
    assert stored["consolidation"]["status"] == "awaiting_cycle"
    assert learning.get_knowledge("Python generators") == []


def test_consolidation_creates_verbatim_pending_candidate_and_keeps_episode(learning_store):
    content = (
        "A Python generator yields values lazily. It suspends execution at each "
        "yield expression and resumes when the next value is requested."
    )
    experience_id = learning.submit_learning_note("Python generators", content)

    result = learning.run_learning_consolidation_cycle(max_items=5)

    assert result["status"] == "completed"
    assert result["processed_count"] == 1
    assert result["created_count"] == 1
    assert result["queued_count"] == 1
    assert result["auto_approved"] is False
    proposal = result["proposals"][0]
    assert proposal["experience_id"] == experience_id
    assert proposal["state"] == "queued_for_review"

    candidate = learning.get_review_item(proposal["knowledge_id"])
    assert candidate["status"] == "pending_review"
    assert candidate["topic"] == "Python generators"
    assert candidate["content"] == content
    assert candidate["origin_experience_id"] == experience_id
    assert any(item.startswith("content_sha256: ") for item in candidate["evidence"])
    assert learning.get_knowledge("Python generators") == []

    original = learning.get_review_item(experience_id)
    assert original["content"] == content
    assert original["consolidation"]["status"] == "queued_for_review"

    repeated = learning.run_learning_consolidation_cycle()
    assert repeated["status"] == "no_candidates"
    assert repeated["created_count"] == 0


def test_consolidation_is_bounded_and_leaves_remaining_notes_for_next_cycle(learning_store):
    ids = [
        learning.submit_learning_note(
            f"Topic {index}",
            f"Owner note {index} with enough detail to preserve as a learning item.",
        )
        for index in range(3)
    ]

    first = learning.run_learning_consolidation_cycle(max_items=2)
    assert first["processed_count"] == 2
    assert first["remaining_count"] == 1
    assert first["has_more"] is True
    assert {item["experience_id"] for item in first["proposals"]} == set(ids[:2])

    second = learning.run_learning_consolidation_cycle(max_items=2)
    assert second["processed_count"] == 1
    assert second["remaining_count"] == 0
    assert second["has_more"] is False
    assert second["proposals"][0]["experience_id"] == ids[2]


def test_capacity_failure_does_not_mark_owner_note_as_consolidated(learning_store, monkeypatch):
    learning.submit_learning_note(
        "Python",
        "Python dictionaries preserve insertion order in modern Python versions.",
    )
    learning.propose_knowledge(
        "Existing protected fact",
        "This existing pending item must not be evicted.",
        "test",
        evidence=[],
    )
    monkeypatch.setattr(learning, "MAX_RECORDS", 1)

    blocked = learning.run_learning_consolidation_cycle()
    assert blocked["status"] == "capacity_reached"
    assert blocked["processed_count"] == 0
    assert blocked["remaining_count"] == 1
    note = next(
        item for item in learning._load()["experiences"]
        if item.get("kind") == "owner_learning_note"
    )
    assert note["consolidation"]["status"] == "awaiting_cycle"

    monkeypatch.setattr(learning, "MAX_RECORDS", 2)
    retry = learning.run_learning_consolidation_cycle()
    assert retry["created_count"] == 1
    assert retry["remaining_count"] == 0


def test_consolidation_rejects_invalid_batch_sizes(learning_store):
    for invalid in (0, 21, True, 1.5, "2"):
        with pytest.raises(ValueError):
            learning.run_learning_consolidation_cycle(max_items=invalid)


def test_learning_status_reports_owner_note_checkpoint_counts(learning_store):
    note_id = learning.submit_learning_note(
        "Databases",
        "A database index improves lookup performance by organizing access paths.",
    )
    before = learning.get_learning_status()
    assert before["owner_learning_notes_awaiting_consolidation"] == 1
    assert before["owner_learning_notes_queued_for_review"] == 0

    learning.run_learning_consolidation_cycle()
    after = learning.get_learning_status()
    assert after["owner_learning_notes_awaiting_consolidation"] == 0
    assert after["owner_learning_notes_queued_for_review"] == 1
    assert learning.get_review_item(note_id)["content"].startswith("A database index")


def test_learning_note_and_consolidation_routes_require_uosa_owner_scope(learning_store):
    assert uosa_security.SCOPE_AUTH_LEVEL["ale.knowledge.submit"] == "L1"

    denied_note = client.post(
        "/api/v1/learning/notes",
        json={
            "topic": "Python",
            "content": "A generator yields values lazily.",
        },
    )
    assert denied_note.status_code == 401

    note_content = "A generator yields values lazily and suspends execution at yield."
    submitted = client.post(
        "/api/v1/learning/notes",
        headers=_headers(),
        json={
            "topic": "Python generators",
            "content": note_content,
        },
    )
    assert submitted.status_code == 200
    note_id = submitted.json()["experience_id"]
    assert submitted.json()["auto_approved"] is False

    denied_cycle = client.post(
        "/api/v1/learning/consolidation/run",
        json={"max_items": 5},
    )
    assert denied_cycle.status_code == 401

    cycle = client.post(
        "/api/v1/learning/consolidation/run",
        headers=_headers(),
        json={"max_items": 5},
    )
    assert cycle.status_code == 200
    payload = cycle.json()
    assert payload["created_count"] == 1
    assert payload["auto_approved"] is False
    candidate_id = payload["proposals"][0]["knowledge_id"]

    queue = client.get(
        "/api/v1/learning/review-queue?kind=knowledge",
        headers=_headers(),
    )
    assert queue.status_code == 200
    candidate = next(
        item for item in queue.json()["items"] if item["id"] == candidate_id
    )
    assert candidate["origin_experience_id"] == note_id
    assert candidate["content"] == note_content

    approved = client.post(
        f"/api/v1/learning/review-queue/{candidate_id}/approve",
        headers=_headers(),
    )
    assert approved.status_code == 200
    assert learning.get_knowledge("Python generators")[0]["id"] == candidate_id
