import json

import pytest

from core import memory
from core.memory_extractor import extract_memories
from core.memory_manager import forget, remember
from core.memory_retriever import retrieve_memories


@pytest.fixture
def memory_file(tmp_path, monkeypatch):
    target = tmp_path / "persistent_memory.json"
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(target))
    monkeypatch.setattr(memory, "MEMORY_ENABLED", True)
    return target


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_missing_store_is_a_valid_empty_store(memory_file):
    assert memory.load_memory() == {"facts": []}


@pytest.mark.parametrize("payload", ["{broken", "[]", "null", '{"facts": "wrong"}'])
def test_corrupt_or_invalid_store_fails_closed(memory_file, payload):
    memory_file.write_text(payload, encoding="utf-8")

    with pytest.raises(memory.MemoryStoreError):
        memory.load_memory()


@pytest.mark.parametrize(
    "fact",
    [
        {},
        {"category": "preference", "key": "car"},
        {"category": "", "key": "car", "value": "coupe"},
        {"category": "preference", "key": " ", "value": "coupe"},
        {"category": "preference", "key": "car", "value": ""},
        {"category": 3, "key": "car", "value": "coupe"},
        {"category": "preference", "key": "car", "value": None},
    ],
)
def test_malformed_fact_fails_closed(memory_file, fact):
    write_json(memory_file, {"facts": [fact]})

    with pytest.raises(memory.MemoryStoreError):
        memory.load_memory()


def test_valid_legacy_records_and_extra_metadata_are_preserved(memory_file):
    original = {
        "facts": [
            {
                "category": "preference",
                "key": "favorite_vehicle",
                "value": "coupe",
                "source": "explicit_user_statement",
                "custom_metadata": {"confidence": 1.0},
            }
        ],
        "schema_version": 1,
    }
    write_json(memory_file, original)

    assert memory.load_memory() == original
    assert memory.get_memory("preference", "favorite_vehicle") == original["facts"]


def test_remember_and_forget_never_overwrite_corrupt_storage(memory_file):
    damaged = '{"facts": ['
    memory_file.write_text(damaged, encoding="utf-8")

    with pytest.raises(memory.MemoryStoreError):
        remember("preference", "favorite_vehicle", "coupe")
    with pytest.raises(memory.MemoryStoreError):
        forget("preference", "favorite_vehicle")

    assert memory_file.read_text(encoding="utf-8") == damaged


def test_save_rejects_invalid_payload_without_replacing_valid_store(memory_file):
    original = {"facts": []}
    write_json(memory_file, original)
    before = memory_file.read_bytes()

    with pytest.raises(memory.MemoryStoreError):
        memory.save_memory({"facts": "not-a-list"})

    assert memory_file.read_bytes() == before


def test_failed_atomic_replace_preserves_previous_store(memory_file, monkeypatch):
    original = {"facts": [{"category": "preference", "key": "vehicle", "value": "coupe"}]}
    write_json(memory_file, original)
    before = memory_file.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(memory.os, "replace", fail_replace)
    with pytest.raises(OSError):
        memory.add_memory("preference", "favorite_anime", "Example Series")

    assert memory_file.read_bytes() == before
    assert list(memory_file.parent.glob("persistent_memory.json.*.tmp")) == []


def test_explicit_clear_is_allowed_to_recover_a_corrupt_store(memory_file):
    memory_file.write_text("{broken", encoding="utf-8")

    memory.clear_memory()

    assert memory.load_memory() == {"facts": []}


def test_retrieval_understands_common_paraphrases():
    facts = [
        {"category": "preference", "key": "favorite_anime", "value": "Example Series"},
        {"category": "preference", "key": "favorite_vehicle", "value": "Example Coupe"},
        {"category": "personal_fact", "key": "field_of_study", "value": "Software Engineering"},
    ]

    assert retrieve_memories("Which anime did I say I enjoyed most?", facts)[0]["key"] == "favorite_anime"
    assert retrieve_memories("Which vehicle do I like?", facts)[0]["key"] == "favorite_vehicle"
    assert retrieve_memories("What degree am I pursuing?", facts)[0]["key"] == "field_of_study"


def test_retrieval_is_bounded_and_does_not_mutate_input():
    facts = [
        {"category": "preference", "key": f"favorite_anime_{index}", "value": f"Series {index}"}
        for index in range(12)
    ]
    original = json.loads(json.dumps(facts))

    result = retrieve_memories("favorite anime", facts, limit=3)

    assert len(result) <= 3
    assert facts == original


def test_extractor_recognizes_natural_current_study_statement():
    candidates = extract_memories("I'm studying Software Engineering at Northbridge University.")

    assert {
        "category": "personal_fact",
        "key": "field_of_study",
        "value": "Software Engineering",
        "operation": "remember",
    } in candidates


def test_extractor_recognizes_concise_response_preference():
    candidates = extract_memories("Remember, I prefer concise explanations.")

    assert {
        "category": "preference",
        "key": "response_style",
        "value": "concise explanations",
        "operation": "remember",
    } in candidates


def test_extractor_recognizes_changed_vehicle_preference():
    candidates = extract_memories("I've changed my mind; my favorite car is now a Supra.")

    assert {
        "category": "preference",
        "key": "favorite_vehicle",
        "value": "a Supra",
        "operation": "remember",
    } in candidates


def test_extractor_does_not_store_ambiguous_or_future_intent():
    assert extract_memories("Maybe one day I will buy a sports car.") == []
    assert extract_memories("I don't know what my favorite anime is.") == []


def test_memory_diagnostic_reports_corruption_without_raw_file_content(memory_file):
    from core import diagnostics

    memory_file.write_text('{"private": "do-not-expose", "facts": "broken"}', encoding="utf-8")
    check = diagnostics._check("persistent_memory", diagnostics._memory_check)

    assert check["status"] == "error"
    assert "do-not-expose" not in check["detail"]

def test_chat_memory_capture_does_not_fail_when_store_is_corrupt(memory_file):
    from core.router import process_memory_candidates

    damaged = '{"facts": ['
    memory_file.write_text(damaged, encoding="utf-8")

    processed = process_memory_candidates(
        "Please remember that my favorite car is a Supra."
    )

    assert processed == []
    assert memory_file.read_text(encoding="utf-8") == damaged


def test_context_marks_corrupt_memory_unavailable_not_empty(memory_file):
    from core.context_manager import build_context

    memory_file.write_text('{"facts": "damaged"}', encoding="utf-8")

    context = build_context("Hello.")

    assert context["memory_status"] == "unavailable"
    assert context["memories"] == []
