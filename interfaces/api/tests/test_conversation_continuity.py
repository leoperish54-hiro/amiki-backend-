import pytest

from core.conversation_awareness import resolve_conversation_awareness
from core.context_manager import build_context


RECENT_EXCHANGE = [
    {"role": "user", "text": "Can you improve persistent memory safely?"},
    {"role": "assistant", "text": "Yes. I would start by validating storage and retrieval."},
]


@pytest.mark.parametrize(
    "reply",
    [
        "Yeah",
        "Yes.",
        "Okay",
        "Exactly",
        "Right",
        "Got it",
        "Sounds good",
        "Go ahead",
        "Please do",
        "Let's do it",
        "What do you think?",
    ],
)
def test_short_acknowledgements_keep_the_immediate_exchange(reply):
    result = resolve_conversation_awareness(reply, RECENT_EXCHANGE)

    assert result["outcome"] == "CONTINUE"
    assert result["supporting_turn_indexes"] == [0, 1]


def test_short_acknowledgement_without_history_reports_insufficient_context():
    result = resolve_conversation_awareness("Yeah", [])

    assert result["outcome"] == "INSUFFICIENT_CONTEXT"


def test_explicit_topic_switch_still_overrides_acknowledgement_cues():
    result = resolve_conversation_awareness(
        "New topic: what is orbital resonance?",
        RECENT_EXCHANGE,
    )

    assert result["outcome"] == "NEW_TOPIC"
    assert result["supporting_turn_indexes"] == []


def test_context_manager_passes_acknowledgement_exchange_to_prompt(monkeypatch):
    monkeypatch.setattr(
        "core.context_manager.load_context",
        lambda: list(RECENT_EXCHANGE),
    )
    monkeypatch.setattr(
        "core.context_manager.get_memory",
        lambda: [],
    )

    context = build_context("Yeah")

    assert context["conversation"] == RECENT_EXCHANGE
    assert context["conversation_awareness"]["outcome"] == "CONTINUE"


def test_context_manager_does_not_leak_previous_exchange_after_topic_switch(monkeypatch):
    monkeypatch.setattr(
        "core.context_manager.load_context",
        lambda: list(RECENT_EXCHANGE),
    )
    monkeypatch.setattr(
        "core.context_manager.get_memory",
        lambda: [],
    )

    context = build_context("New topic: explain orbital resonance.")

    assert context["conversation"] == []
    assert context["conversation_awareness"]["outcome"] == "NEW_TOPIC"



def test_continue_falls_back_to_the_latest_exchange_when_no_task_label_matches():
    result = resolve_conversation_awareness("Continue", RECENT_EXCHANGE)

    assert result["outcome"] == "CONTINUE"
    assert result["supporting_turn_indexes"] == [0, 1]
