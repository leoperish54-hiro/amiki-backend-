"""Deterministic, bounded conversation-reference resolution.

No LLM classification or persistent inferred-topic state is used. Results point
to source-turn indexes in the supplied history; callers choose which turns to
include in the model prompt.
"""
import re

_DEFAULT_HISTORY_LIMIT = 20
_RESET_RE = re.compile(r"^(?:new topic\b|unrelated question\b|different topic\b|let['’]?s switch(?: to\b|\b)|switch topics?\b|change of topic\b)", re.I)
_CONTINUE_RE = re.compile(r"^(?:continue\b|go on\b|carry on\b|keep going\b|next step\b|what['’]?s next\b|what is next\b|pick up where we left off\b|resume\b)", re.I)
_ELLIPTICAL_RE = re.compile(
    r"^(?:why\??|how so\??|how come\??|"
    r"what about (?:the )?(?:cost|price|that|this|it)\??|"
    r"and then\??|then what\??|"
    r"yes|yeah|yep|yup|no|nope|okay|ok|exactly|right|correct|"
    r"got it|understood|sure|absolutely|sounds good|that works|"
    r"fair enough|i agree|i see|go ahead|please do|"
    r"let['’]?s do it|what do you think)\s*[.!?]?$",
    re.I,
)
_ORDINAL_RE = re.compile(r"\b(first|1st|second|2nd|third|3rd|fourth|4th)\b", re.I)
_OPTION_RE = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)]|option\s+[1-4]\b)\s+\S", re.I)
_REFERENCE_RE = re.compile(r"\b(?:that one|this one|that option|this option|the option|the first one|the second one|the third one|the fourth one|the first option|the second option|the third option|the fourth option|what did i say earlier|what you said earlier|earlier|it|that|this|those|these)\b", re.I)
_TASK_RE = re.compile(r"\b(?:build|write|explain|plan|implement|design|research|learn|create|fix|analyse|analyze|compare|teach|review|develop|investigate|summarize|summarise)\b", re.I)
_COMPLETION_RE = re.compile(r"\b(?:done|completed|finished|that solves it|problem solved|that is all|that's all|we're finished)\b", re.I)


def _valid_turns(history, limit):
    if not isinstance(history, list) or not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    valid = []
    for index, item in enumerate(history):
        if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("text"), str) and item["text"].strip():
            valid.append((index, item["role"], item["text"].strip()))
    return valid[-limit:]


def _result(outcome, reason, indexes=()):
    return {"outcome": outcome, "reason": reason, "supporting_turn_indexes": list(indexes)}


def _exchange_indexes(turns):
    last_assistant = next((p for p in range(len(turns) - 1, -1, -1) if turns[p][1] == "assistant"), None)
    if last_assistant is None:
        return []
    previous_user = next((p for p in range(last_assistant - 1, -1, -1) if turns[p][1] == "user"), None)
    positions = ([previous_user] if previous_user is not None else []) + [last_assistant]
    return [turns[p][0] for p in positions]


def resolve_conversation_awareness(message, history, limit=_DEFAULT_HISTORY_LIMIT):
    """Resolve clear continuation/topic/reference cues against bounded history."""
    if not isinstance(message, str) or not message.strip():
        return _result("INSUFFICIENT_CONTEXT", "empty_message")
    text = message.strip()
    turns = _valid_turns(history, limit)

    # Explicit reset always wins, even if it repeats an old topic keyword.
    if _RESET_RE.search(text):
        return _result("NEW_TOPIC", "explicit_topic_switch")

    if _CONTINUE_RE.search(text):
        tasks = []
        for position, (index, role, turn_text) in enumerate(turns):
            if role != "user" or not _TASK_RE.search(turn_text):
                continue
            next_user = next(
                (candidate for candidate in range(position + 1, len(turns))
                 if turns[candidate][1] == "user"),
                len(turns),
            )
            assistant_replies = [
                turns[candidate][2]
                for candidate in range(position + 1, next_user)
                if turns[candidate][1] == "assistant"
            ]
            completed = any(_COMPLETION_RE.search(reply) for reply in assistant_replies)
            if not completed:
                supporting_indexes = [
                    turns[candidate][0]
                    for candidate in range(position, next_user)
                ]
                tasks.append((supporting_indexes, turn_text))
        if not tasks:
            indexes = _exchange_indexes(turns)
            if indexes:
                return _result(
                    "CONTINUE",
                    "immediate_exchange_continuation",
                    indexes,
                )
            return _result("INSUFFICIENT_CONTEXT", "no_recoverable_active_task")
        if len(tasks) > 1:
            supporting_indexes = [
                index
                for indexes, _ in tasks[-3:]
                for index in indexes
            ]
            return _result(
                "AMBIGUOUS",
                "multiple_plausible_active_tasks",
                supporting_indexes,
            )
        return _result("CONTINUE", "explicit_continuation", tasks[-1][0])

    if _ELLIPTICAL_RE.fullmatch(text):
        indexes = _exchange_indexes(turns)
        if not indexes:
            return _result("INSUFFICIENT_CONTEXT", "missing_immediate_exchange")
        return _result("CONTINUE", "elliptical_follow_up", indexes)

    if _REFERENCE_RE.search(text):
        ordinal = _ORDINAL_RE.search(text)
        option_sources = [(i, t, len(_OPTION_RE.findall(t))) for i, _, t in turns if _OPTION_RE.search(t)]
        if ordinal:
            requested = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}[ordinal.group(1).casefold()]
            if len(option_sources) != 1:
                if not option_sources:
                    return _result("INSUFFICIENT_CONTEXT", "no_recoverable_option_list")
                return _result("AMBIGUOUS", "multiple_option_lists", [item[0] for item in option_sources])
            index, _, count = option_sources[0]
            if requested > count:
                return _result("INSUFFICIENT_CONTEXT", "ordinal_out_of_range", [index])
            return _result("CONTINUE", "unique_option_list", [index])
        if len(option_sources) > 1:
            return _result("AMBIGUOUS", "multiple_plausible_option_lists", [item[0] for item in option_sources])
        if len(option_sources) == 1:
            index, _, count = option_sources[0]
            if count == 1:
                return _result("CONTINUE", "unique_option", [index])
            return _result("AMBIGUOUS", "multiple_options_in_list", [index])
        indexes = _exchange_indexes(turns)
        if not indexes:
            return _result("INSUFFICIENT_CONTEXT", "no_recoverable_antecedent")
        latest_answer = next((t for _, role, t in reversed(turns) if role == "assistant"), "")
        if _OPTION_RE.search(latest_answer):
            return _result("AMBIGUOUS", "competing_referents_in_latest_answer", indexes)
        return _result("CONTINUE", "immediate_exchange_reference", indexes)

    return _result("NEW_TOPIC", "no_follow_up_signal")
