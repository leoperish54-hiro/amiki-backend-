def classify_memory(candidate):
    """
    Classify a memory candidate for long-term storage.

    The classifier validates the requested memory operation but does
    not perform the operation itself.
    """

    category = candidate.get("category")
    operation = candidate.get("operation", "remember")

    # Explicit forget operation
    if operation == "forget":
        if category in {"personal_fact", "preference", "project_fact"}:
            return {
                "decision": "forget",
                "class": "explicit_forget"
            }

        return {
            "decision": "reject",
            "class": "uncertain"
        }

    # Unknown operations must not reach the memory manager.
    if operation != "remember":
        return {
            "decision": "reject",
            "class": "invalid_operation"
        }

    if category == "personal_fact":
        return {
            "decision": "remember",
            "class": "explicit_fact"
        }

    if category == "preference":
        return {
            "decision": "remember",
            "class": "explicit_preference"
        }

    if category == "project_fact":
        return {
            "decision": "remember",
            "class": "explicit_project_fact"
        }

    if category == "temporary":
        return {
            "decision": "reject",
            "class": "temporary_state"
        }

    if category == "future_intent":
        return {
            "decision": "reject",
            "class": "future_intent"
        }

    if category == "casual_statement":
        return {
            "decision": "reject",
            "class": "casual_statement"
        }

    return {
        "decision": "reject",
        "class": "uncertain"
    }
