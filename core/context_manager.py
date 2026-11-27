from core.context import load_context
from core.conversation_awareness import resolve_conversation_awareness
from core.memory import MEMORY_ENABLED, MemoryStoreError, get_memory
from core.memory_retriever import retrieve_memories


def build_context(message, conversation_id=None):
    """
    Build a unified read-only context package.

    Conversation awareness is resolved only against the selected conversation's
    bounded transcript. Long-term memories remain a separate retrieval domain.
    """
    try:
        memories = get_memory()
    except MemoryStoreError:
        # Keep the conversation usable while preserving the distinction
        # between an empty store and a store that cannot be read safely.
        relevant_memories = []
        memory_status = "unavailable"
    else:
        relevant_memories = retrieve_memories(message, memories)
        memory_status = "available" if MEMORY_ENABLED else "disabled"

    history = (
        load_context()
        if conversation_id is None
        else load_context(conversation_id=conversation_id)
    )
    awareness = resolve_conversation_awareness(message, history)

    if awareness["outcome"] == "CONTINUE":
        supported_indexes = set(awareness["supporting_turn_indexes"])
        relevant_conversation = [
            item for index, item in enumerate(history)
            if index in supported_indexes
        ]
    elif awareness["outcome"] == "AMBIGUOUS":
        supported_indexes = set(awareness["supporting_turn_indexes"])
        relevant_conversation = [
            item for index, item in enumerate(history)
            if index in supported_indexes
        ]
    elif awareness["outcome"] == "INSUFFICIENT_CONTEXT":
        # Keep only the latest bounded exchange as raw evidence. The prompt
        # explicitly tells the model not to infer a missing antecedent from it.
        relevant_conversation = history[-2:]
    else:
        # A clear topic switch must not inherit unrelated prior turns.
        relevant_conversation = []

    return {
        "memories": relevant_memories,
        "memory_status": memory_status,
        "conversation": relevant_conversation,
        "conversation_awareness": awareness,
    }
