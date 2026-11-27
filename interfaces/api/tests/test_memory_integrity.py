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



def test_metadata_survives_add_and_delete_operations(memory_file):
    original = {
        "schema_version": 1,
        "owner_scope": "local",
        "facts": [
            {
                "category": "preference",
                "key": "favorite_vehicle",
                "value": "coupe",
                "provenance": {"kind": "explicit"},
            }
        ],
    }
    write_json(memory_file, original)

    memory.add_memory("preference", "response_style", "concise answers")
    after_add = memory.load_memory()
    assert after_add["schema_version"] == 1
    assert after_add["owner_scope"] == "local"
    assert after_add["facts"][0]["provenance"] == {"kind": "explicit"}

    memory.delete_memory("preference", "response_style")
    after_delete = memory.load_memory()
    assert after_delete["schema_version"] == 1
    assert after_delete["owner_scope"] == "local"
    assert after_delete["facts"] == original["facts"]


def test_missing_facts_field_is_not_silently_migrated(memory_file):
    write_json(memory_file, {"schema_version": 1})

    with pytest.raises(memory.MemoryStoreError):
        memory.load_memory()


def test_invalid_utf8_store_is_reported_without_data_loss(memory_file):
    damaged = b'{"facts": []}\xff'
    memory_file.write_bytes(damaged)

    with pytest.raises(memory.MemoryStoreError):
        memory.load_memory()

    assert memory_file.read_bytes() == damaged


def test_manager_remember_update_and_forget_round_trip(memory_file):
    assert remember("preference", "favorite_vehicle", "coupe") is True
    assert remember("preference", "favorite_vehicle", "sedan") is True
    assert memory.get_memory("preference", "favorite_vehicle") == [
        {"category": "preference", "key": "favorite_vehicle", "value": "sedan"}
    ]

    assert forget("preference", "favorite_vehicle") is True
    assert memory.get_memory("preference", "favorite_vehicle") == []


def test_extractor_recognizes_explicit_interest_and_crowd_preference():
    interest = extract_memories("I'm really into cars, especially the Supra.")
    crowd = extract_memories("I don't like being in huge crowds.")

    assert {
        "category": "preference",
        "key": "interests",
        "value": "cars, especially the Supra",
        "operation": "remember",
    } in interest
    assert {
        "category": "preference",
        "key": "dislikes_large_crowds",
        "value": "dislikes large crowds",
        "operation": "remember",
    } in crowd


def test_retrieval_finds_interest_and_style_preferences():
    facts = [
        {"category": "preference", "key": "interests", "value": "cars and space"},
        {"category": "preference", "key": "response_style", "value": "concise explanations"},
    ]

    assert retrieve_memories("What are my interests?", facts)[0]["key"] == "interests"
    assert retrieve_memories("How do I prefer explanations?", facts)[0]["key"] == "response_style"



def test_prompt_explicitly_discloses_unavailable_persistent_memory(memory_file, monkeypatch):
    from core import prompt
    from core.context_manager import build_context

    memory_file.write_text('{"facts": "damaged"}', encoding="utf-8")
    monkeypatch.setattr(prompt, "_active_learning_project", lambda *_args: None)
    monkeypatch.setattr(prompt, "get_relevant_knowledge", lambda *_args: [])
    monkeypatch.setattr(prompt, "build_self_knowledge_contract", lambda: {})

    context = build_context("Hello.")
    rendered = prompt.build_prompt("Hello.", _context=context)

    assert "Persistent memory is unavailable" in rendered
    assert "do not claim that memory is empty" in rendered


def test_memory_api_returns_service_unavailable_for_corrupt_store(monkeypatch):
    from fastapi import HTTPException
    from interfaces.api.routes import memory as memory_routes

    def fail_recall():
        raise memory.MemoryStoreError("internal detail must not escape")

    monkeypatch.setattr(memory_routes, "recall", fail_recall)

    with pytest.raises(HTTPException) as raised:
        memory_routes.memory_endpoint()

    assert raised.value.status_code == 503
    assert raised.value.detail == (
        "Persistent memory is unavailable. No data was changed."
    )


def test_concurrent_writes_do_not_drop_facts(memory_file):
    from concurrent.futures import ThreadPoolExecutor

    def write_fact(index):
        memory.add_memory("preference", f"preference_{index}", f"value_{index}")

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(write_fact, range(18)))

    facts = memory.get_memory()
    assert len(facts) == 18
    assert {fact["key"] for fact in facts} == {
        f"preference_{index}" for index in range(18)
    }


def test_persistent_memory_file_is_created_with_restrictive_permissions(memory_file):
    import stat

    memory.add_memory("preference", "response_style", "concise answers")

    assert stat.S_IMODE(memory_file.stat().st_mode) == 0o600


def test_extractor_handles_non_string_input_safely():
    assert extract_memories(None) == []
    assert extract_memories(42) == []



def test_compound_query_keeps_exact_and_curated_alias_matches():
    facts = [
        {"category": "preference", "key": "favorite_anime", "value": "Example Series"},
        {"category": "preference", "key": "favorite_vehicle", "value": "Example Coupe"},
        {"category": "personal_fact", "key": "field_of_study", "value": "Software Engineering"},
    ]

    results = retrieve_memories(
        "What is my favorite anime and what degree am I pursuing?",
        facts,
    )

    assert [fact["key"] for fact in results] == [
        "favorite_anime",
        "field_of_study",
    ]



def test_general_memory_question_returns_bounded_memory_overview():
    facts = [
        {"category": "personal_fact", "key": f"fact_{index}", "value": f"value_{index}"}
        for index in range(8)
    ]

    results = retrieve_memories("What do you remember about me?", facts)

    assert results == facts[-5:]



def test_forget_does_not_claim_success_when_memory_is_disabled(monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_ENABLED", False)

    assert forget("preference", "favorite_vehicle") is False



def test_exact_memory_key_suppresses_weaker_partial_matches():
    facts = [
        {"category": "preference", "key": "favorite_anime", "value": "Example Series"},
        {"category": "preference", "key": "favorite_car", "value": "Example Coupe"},
    ]

    results = retrieve_memories("What is my favorite anime?", facts)

    assert [fact["key"] for fact in results] == ["favorite_anime"]



def test_extractor_preserves_existing_name_and_anime_contracts():
    assert extract_memories("My name is Morgan.") == [
        {
            "category": "personal_fact",
            "key": "name",
            "value": "Morgan",
            "operation": "remember",
        }
    ]
    assert extract_memories("My favorite anime is Example Series.") == [
        {
            "category": "preference",
            "key": "favorite_anime",
            "value": "Example Series",
            "operation": "remember",
        }
    ]


def test_extractor_preserves_existing_correction_and_preference_contracts():
    assert extract_memories(
        "My favorite anime used to be Example Series, but now it's Another Series."
    ) == [
        {
            "category": "preference",
            "key": "favorite_anime",
            "value": "Another Series",
            "operation": "remember",
        }
    ]
    assert extract_memories("I prefer Python over Java.") == [
        {
            "category": "preference",
            "key": "preferred_programming_language",
            "value": "Python",
            "operation": "remember",
        }
    ]


def test_extractor_preserves_explicit_like_and_forget_contracts():
    assert extract_memories("I like sleeping.") == [
        {
            "category": "preference",
            "key": "likes",
            "value": "sleeping",
            "operation": "remember",
        }
    ]
    assert extract_memories("I like this song.") == []
    assert extract_memories("Forget my favorite anime.") == [
        {
            "category": "preference",
            "key": "favorite_anime",
            "value": None,
            "operation": "forget",
        }
    ]
    assert extract_memories("I don't have a favorite anime anymore.") == [
        {
            "category": "preference",
            "key": "favorite_anime",
            "value": None,
            "operation": "forget",
        }
    ]


def test_extractor_does_not_promote_temporary_activity_to_preference():
    assert extract_memories("I'm watching Example Series right now.") == []


def test_read_error_is_not_reported_as_empty_memory(memory_file, monkeypatch):
    import builtins

    original_open = builtins.open

    def fail_memory_read(path, *args, **kwargs):
        if str(path) == str(memory_file):
            raise PermissionError("private path must not be exposed")
        return original_open(path, *args, **kwargs)

    write_json(memory_file, {"facts": []})
    monkeypatch.setattr(builtins, "open", fail_memory_read)

    with pytest.raises(memory.MemoryStoreError, match="could not be read"):
        memory.load_memory()



def test_manager_reports_false_when_write_does_not_persist(monkeypatch):
    import core.memory_manager as manager

    monkeypatch.setattr(memory, "MEMORY_ENABLED", True)
    monkeypatch.setattr(manager, "get_memory", lambda *_args: [])
    monkeypatch.setattr(manager, "add_memory", lambda *_args: None)

    assert manager.remember("personal_fact", "preferred_name", "Morgan") is False


def test_manager_does_not_claim_remember_success_when_storage_is_disabled(monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_ENABLED", False)

    assert remember("personal_fact", "preferred_name", "Morgan") is False
