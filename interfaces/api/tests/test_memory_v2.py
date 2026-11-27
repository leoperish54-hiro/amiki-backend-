import multiprocessing
import os

import pytest

from core import memory
from core.memory_consolidator import analyse_memory_clusters
from core.memory_retriever import retrieve_memories


def _write_from_process(index):
    from core.memory import add_memory
    add_memory(
        "preference",
        f"process_fact_{index}",
        f"value_{index}",
        provenance={"source_type": "multiprocess_test"},
        confidence=0.8,
    )


def test_fact_updates_preserve_temporal_history_and_supersession(tmp_path, monkeypatch):
    monkeypatch.setitem(memory.MEMORY_CONFIG, "database_file", str(tmp_path / "temporal.sqlite3"))
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(tmp_path / "legacy.json"))

    memory.add_memory("preference", "favorite_vehicle", "coupe")
    old = memory.get_memory("preference", "favorite_vehicle")[0]
    memory.add_memory("preference", "favorite_vehicle", "sports car")

    current = memory.get_memory("preference", "favorite_vehicle")
    history = memory.get_memory_history("preference", "favorite_vehicle")

    assert len(current) == 1
    assert current[0]["value"] == "sports car"
    assert len(history) == 2
    previous = next(fact for fact in history if fact["id"] == old["id"])
    latest = next(fact for fact in history if fact["value"] == "sports car")
    assert previous["status"] == "superseded"
    assert previous["valid_to"] is not None
    assert previous["superseded_by"] == latest["id"]
    assert latest["status"] == "active"


def test_memory_provenance_and_confidence_are_persisted(tmp_path, monkeypatch):
    monkeypatch.setitem(memory.MEMORY_CONFIG, "database_file", str(tmp_path / "provenance.sqlite3"))
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(tmp_path / "legacy.json"))

    memory.add_memory(
        "personal_fact",
        "field_of_study",
        "Software Engineering",
        provenance={"capture_mode": "explicit_user_statement"},
        confidence=0.97,
        source_text="Please remember that I study Software Engineering.",
        source_type="user_statement",
        extraction_method="explicit_pattern",
        correlation_id="test-turn-42",
    )

    fact = memory.get_memory("personal_fact", "field_of_study")[0]
    assert fact["confidence"] == pytest.approx(0.97)
    assert fact["source_type"] == "user_statement"
    assert fact["extraction_method"] == "explicit_pattern"
    assert fact["provenance"]["capture_mode"] == "explicit_user_statement"
    assert fact["provenance"]["source_text"].startswith("Please remember")
    assert fact["provenance"]["correlation_id"] == "test-turn-42"


def test_same_fact_reinforcement_adds_evidence_without_duplicate_active_fact(tmp_path, monkeypatch):
    monkeypatch.setitem(memory.MEMORY_CONFIG, "database_file", str(tmp_path / "reinforce.sqlite3"))
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(tmp_path / "legacy.json"))

    memory.add_memory("preference", "response_style", "concise answers", confidence=0.7)
    memory.add_memory(
        "preference",
        "response_style",
        "concise answers",
        confidence=0.95,
        provenance={"capture_mode": "confirmed"},
    )

    facts = memory.get_memory("preference", "response_style")
    assert len(facts) == 1
    assert facts[0]["confidence"] == pytest.approx(0.95)
    assert len(facts[0]["provenance"]["evidence"]) == 1
    events = memory.get_memory_events("preference", "response_style")
    assert {event["event_type"] for event in events} == {"created", "reinforced"}


def test_legacy_json_is_imported_once_and_retained_as_recovery_copy(tmp_path, monkeypatch):
    legacy = tmp_path / "persistent_memory.json"
    legacy.write_text(
        '{"schema_version":1,"facts":[{"category":"preference","key":"favorite_vehicle","value":"coupe"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(legacy))
    monkeypatch.setitem(memory.MEMORY_CONFIG, "database_file", str(tmp_path / "migrated.sqlite3"))

    assert memory.get_memory("preference", "favorite_vehicle")[0]["value"] == "coupe"
    original_bytes = legacy.read_bytes()
    memory.add_memory("preference", "favorite_vehicle", "sedan")
    assert legacy.read_bytes() == original_bytes
    assert memory.get_memory("preference", "favorite_vehicle")[0]["value"] == "sedan"
    history = memory.get_memory_history("preference", "favorite_vehicle")
    assert len(history) == 2


def test_sqlite_database_uses_wal_and_restrictive_permissions(tmp_path, monkeypatch):
    import sqlite3
    import stat

    path = tmp_path / "secure.sqlite3"
    monkeypatch.setitem(memory.MEMORY_CONFIG, "database_file", str(path))
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(tmp_path / "legacy.json"))

    memory.add_memory("preference", "response_style", "concise")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        connection.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_multiple_processes_can_write_without_lost_updates(tmp_path, monkeypatch):
    path = tmp_path / "multiprocess.sqlite3"
    monkeypatch.setitem(memory.MEMORY_CONFIG, "database_file", str(path))
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(tmp_path / "legacy.json"))

    # Linux CI uses fork; child processes inherit the isolated test DB path.
    context = multiprocessing.get_context("fork")
    workers = [context.Process(target=_write_from_process, args=(index,)) for index in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0

    facts = memory.get_memory()
    assert len(facts) == 8
    assert {fact["key"] for fact in facts} == {f"process_fact_{index}" for index in range(8)}


def test_semantic_embedding_can_retrieve_without_lexical_overlap():
    facts = [
        {
            "category": "personal_fact",
            "key": "education_plan",
            "value": "Software Engineering at university",
            "_embedding": [0.98, 0.20],
        },
        {
            "category": "preference",
            "key": "favorite_vehicle",
            "value": "sports car",
            "_embedding": [0.0, 1.0],
        },
    ]

    results = retrieve_memories(
        "Which academic path am I following?",
        facts,
        query_embedding=[1.0, 0.0],
    )

    assert results[0]["key"] == "education_plan"


def test_consolidation_reports_duplicates_conflicts_and_related_facts():
    facts = [
        {"category": "preference", "key": "favorite_vehicle", "value": "sports car", "_embedding": [1.0, 0.0]},
        {"category": "preference", "key": "favorite_vehicle", "value": "sedan", "_embedding": [0.99, 0.01]},
        {"category": "preference", "key": "dream_car", "value": "sports car", "_embedding": [0.99, 0.01]},
        {"category": "project_fact", "key": "related_interest", "value": "automotive engineering", "_embedding": [0.80, 0.60]},
        {"category": "project_fact", "key": "unrelated_project", "value": "build a compiler", "_embedding": [0.0, 1.0]},
    ]

    report = analyse_memory_clusters(facts)
    assert report["duplicates"]
    assert report["contradictions"]
    assert report["related"]
    assert report["automatic_changes"] == 0


def test_forgetting_is_a_temporal_event_not_physical_deletion(tmp_path, monkeypatch):
    monkeypatch.setitem(memory.MEMORY_CONFIG, "database_file", str(tmp_path / "forget.sqlite3"))
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(tmp_path / "legacy.json"))

    memory.add_memory("preference", "favorite_vehicle", "coupe")
    assert memory.delete_memory("preference", "favorite_vehicle") is True
    assert memory.get_memory("preference", "favorite_vehicle") == []
    history = memory.get_memory_history("preference", "favorite_vehicle")
    assert history[0]["status"] == "forgotten"
    assert memory.get_memory_events("preference", "favorite_vehicle")[0]["event_type"] == "forgotten"
