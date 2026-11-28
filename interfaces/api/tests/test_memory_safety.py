import pytest
from fastapi import HTTPException

from core import memory
from interfaces.api.routes import memory as memory_routes


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_ENABLED", True)
    monkeypatch.setattr(memory, "LONG_TERM_FILE", str(tmp_path / "persistent_memory.json"))
    monkeypatch.setitem(memory.MEMORY_CONFIG, "database_file", str(tmp_path / "persistent_memory.sqlite3"))
    return tmp_path


def test_snapshot_save_preserves_temporal_transitions_and_metadata(isolated_memory):
    memory.add_memory("preference", "favorite_vehicle", "coupe")
    memory.add_memory("preference", "response_style", "detailed answers")
    previous_vehicle = memory.get_memory("preference", "favorite_vehicle")[0]
    previous_style = memory.get_memory("preference", "response_style")[0]

    memory.save_memory({
        "schema_version": 1,
        "owner_scope": "local",
        "facts": [
            {
                "category": "preference",
                "key": "favorite_vehicle",
                "value": "Toyota Supra MK5",
                "confidence": 0.95,
                "provenance": {"capture_mode": "snapshot_test"},
            },
            {
                "category": "personal_fact",
                "key": "field_of_study",
                "value": "Software Engineering",
            },
        ],
    })

    active = memory.get_memory()
    assert {(fact["category"], fact["key"], fact["value"]) for fact in active} == {
        ("preference", "favorite_vehicle", "Toyota Supra MK5"),
        ("personal_fact", "field_of_study", "Software Engineering"),
    }

    vehicle_history = memory.get_memory_history("preference", "favorite_vehicle")
    old_vehicle = next(fact for fact in vehicle_history if fact["id"] == previous_vehicle["id"])
    new_vehicle = next(fact for fact in vehicle_history if fact["value"] == "Toyota Supra MK5")
    assert old_vehicle["status"] == "superseded"
    assert old_vehicle["superseded_by"] == new_vehicle["id"]

    style_history = memory.get_memory_history("preference", "response_style")
    removed_style = next(fact for fact in style_history if fact["id"] == previous_style["id"])
    assert removed_style["status"] == "forgotten"
    assert memory.get_memory_events("preference", "response_style")[0]["event_type"] == "forgotten"

    loaded = memory.load_memory()
    assert loaded["schema_version"] == 1
    assert loaded["owner_scope"] == "local"


def test_snapshot_save_of_unchanged_fact_does_not_create_duplicate_history(isolated_memory):
    memory.add_memory("preference", "favorite_vehicle", "Toyota Supra MK5")
    original = memory.get_memory("preference", "favorite_vehicle")[0]
    events_before = memory.get_memory_events("preference", "favorite_vehicle")

    memory.save_memory({
        "facts": [
            {
                "category": "preference",
                "key": "favorite_vehicle",
                "value": "Toyota Supra MK5",
            }
        ]
    })

    current = memory.get_memory("preference", "favorite_vehicle")
    events_after = memory.get_memory_events("preference", "favorite_vehicle")
    assert len(current) == 1
    assert current[0]["id"] == original["id"]
    assert len(events_after) == len(events_before)


def test_snapshot_save_rejects_duplicate_category_key_pairs(isolated_memory):
    with pytest.raises(ValueError, match="duplicate category/key"):
        memory.save_memory({
            "facts": [
                {"category": "preference", "key": "favorite_vehicle", "value": "coupe"},
                {"category": "preference", "key": "favorite_vehicle", "value": "sedan"},
            ]
        })
    assert memory.get_memory() == []


def test_persistent_clear_endpoint_returns_503_on_storage_failure(monkeypatch):
    def fail_clear():
        raise memory.MemoryStoreError("private storage detail")

    monkeypatch.setattr(memory_routes, "clear_memory", fail_clear)
    with pytest.raises(HTTPException) as raised:
        memory_routes.clear_persistent_memory_endpoint()

    assert raised.value.status_code == 503
    assert raised.value.detail == "Persistent memory could not be safely cleared."
    assert "private storage detail" not in raised.value.detail


def test_chat_memory_capture_does_not_persist_the_entire_user_message(monkeypatch):
    from core import router

    captured = []

    class Observer:
        def emit(self, _event):
            return None

    def capture_remember(*args, **kwargs):
        captured.append((args, kwargs))
        return True

    monkeypatch.setattr(router, "remember", capture_remember)
    monkeypatch.setattr(router, "get_observability", lambda: Observer())

    message = (
        "Please remember that my favorite car is a Supra. "
        "My password is swordfish."
    )
    processed = router.process_memory_candidates(message)

    assert len(processed) == 1
    assert len(captured) == 1
    args, kwargs = captured[0]
    assert args == ("preference", "favorite_vehicle", "a Supra")
    assert "source_text" not in kwargs
    assert "swordfish" not in repr(kwargs)
    assert message not in repr(kwargs)
