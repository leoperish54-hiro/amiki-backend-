import re


def extract_memories(message):
    """
    Extract explicit user facts that may be worth remembering.

    Returns a list of memory candidates.
    """

    memories = []
    if not isinstance(message, str) or not message.strip():
        return memories
    text = message.strip()

    # Explicit remember prefixes may wrap a recognisable statement.
    # This is deliberately not a generic "store the entire message" rule.
    text = re.sub(
        r"^(?:please\s+)?remember(?:\s+that|,)?\s+",
        "",
        text,
        count=1,
        flags=re.IGNORECASE
    )

    # Preferred name
    match = re.search(
        r"\bmy preferred name is ([A-Za-z][A-Za-z0-9_-]*)\b",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "personal_fact",
            "key": "name",
            "value": match.group(1),
            "operation": "remember"
        })

    # Name
    match = re.search(
        r"\bmy name is ([A-Za-z][A-Za-z0-9_-]*)\b",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "personal_fact",
            "key": "name",
            "value": match.group(1),
            "operation": "remember"
        })

    # Favorite anime forget
    if re.search(
        r"\bforget\s+(?:about\s+)?my favorite anime\b",
        text,
        re.IGNORECASE
    ):
        memories.append({
            "category": "preference",
            "key": "favorite_anime",
            "value": None,
            "operation": "forget"
        })
        return memories

    if re.search(
        r"\bI do not have a favorite anime anymore\b|"
        r"\bI don't have a favorite anime anymore\b",
        text,
        re.IGNORECASE
    ):
        memories.append({
            "category": "preference",
            "key": "favorite_anime",
            "value": None,
            "operation": "forget"
        })
        return memories

    # Reject negative favorite-anime statements
    if re.search(
        r"\bmy favorite anime (?:isn't|is not)\b",
        text,
        re.IGNORECASE
    ):
        return memories

    # Favorite anime correction
    match = re.search(
        r"\bused to be my favorite anime\b.*?"
        r"\bbut now (?:it's|it is)\s+(.+?)(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "preference",
            "key": "favorite_anime",
            "value": match.group(1).strip(),
            "operation": "remember"
        })
    else:
        # "My favorite anime used to be X, but now it's Y."
        match = re.search(
            r"\bmy favorite anime used to be\s+.+?"
            r"\bbut now (?:it's|it is)\s+(.+?)(?:[.!?]|$)",
            text,
            re.IGNORECASE
        )

        if match:
            memories.append({
                "category": "preference",
                "key": "favorite_anime",
                "value": match.group(1).strip(),
                "operation": "remember"
            })
        else:
            # Favorite anime correction using "but now I prefer"
            match = re.search(
                r"\bmy favorite anime is\s+.+?"
                r"\bbut now I prefer\s+(.+?)(?:[.!?]|$)",
                text,
                re.IGNORECASE
            )

            if match:
                memories.append({
                    "category": "preference",
                    "key": "favorite_anime",
                    "value": match.group(1).strip(),
                    "operation": "remember"
                })
            else:
                # Favorite anime with a temporal "now" marker
                match = re.search(
                    r"\bmy favorite anime is\s+(.+?)\s+now(?:[.!?]|$)",
                    text,
                    re.IGNORECASE
                )

                if match:
                    memories.append({
                        "category": "preference",
                        "key": "favorite_anime",
                        "value": match.group(1).strip(),
                        "operation": "remember"
                    })
                else:
                    # Normal favorite anime
                    match = re.search(
                        r"\bmy favorite anime is\s+(.+?)(?:[.!?]|$)",
                        text,
                        re.IGNORECASE
                    )

                    if match:
                        memories.append({
                            "category": "preference",
                            "key": "favorite_anime",
                            "value": match.group(1).strip(),
                            "operation": "remember"
                        })

    # Favorite programming language
    match = re.search(
        r"\bmy favorite programming language is\s+(.+?)(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "preference",
            "key": "favorite_programming_language",
            "value": match.group(1).strip(),
            "operation": "remember"
        })

    # Explicit response-style preference
    match = re.search(
        r"\bI prefer honest, evidence-based answers\b",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "preference",
            "key": "response_style",
            "value": "honest, evidence-based answers",
            "operation": "remember"
        })

    # Explicit programming language preference
    match = re.search(
        r"\bI prefer\s+([A-Za-z][A-Za-z0-9+#.-]*)\s+over\s+"
        r"([A-Za-z][A-Za-z0-9+#.-]*)(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "preference",
            "key": "preferred_programming_language",
            "value": match.group(1).strip(),
            "operation": "remember"
        })

    # Explicit "I like" preference
    match = re.search(
        r"\bI like\s+(sleeping|anime|coding|programming|reading|gaming)"
        r"\b(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "preference",
            "key": "likes",
            "value": match.group(1).strip(),
            "operation": "remember"
        })

    # Concrete education plan (not a speculative long-term ambition)
    match = re.search(
        r"\bI am preparing to study ([A-Za-z][A-Za-z &-]*?) at ([A-Za-z][A-Za-z0-9 &-]*?)(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "personal_fact",
            "key": "education_plan",
            "value": f"{match.group(1).strip()} at {match.group(2).strip()}",
            "operation": "remember"
        })

    # Study / field
    match = re.search(
        r"\bI study ([A-Za-z][A-Za-z0-9 &-]*?)(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "personal_fact",
            "key": "field_of_study",
            "value": match.group(1).strip(),
            "operation": "remember"
        })

    # Current study status, including common conversational phrasing.
    match = re.search(
        r"\bI(?: am|'m|’m) studying\s+(.+?)(?:\s+at\s+.+)?[.!?]?$",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "personal_fact",
            "key": "field_of_study",
            "value": match.group(1).strip(),
            "operation": "remember"
        })

    # A changed favorite vehicle is a current preference, not a future plan.
    match = re.search(
        r"\bmy (?:favorite|favourite) (?:car|vehicle) is(?: now)?\s+(.+?)(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "preference",
            "key": "favorite_vehicle",
            "value": match.group(1).strip(),
            "operation": "remember"
        })

    # Explicit response-style preferences.
    match = re.search(
        r"\bI prefer\s+(concise explanations|brief explanations|short answers|"
        r"concise answers|detailed explanations)(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "preference",
            "key": "response_style",
            "value": match.group(1).strip().lower(),
            "operation": "remember"
        })

    # Clear likes/dislikes are stored as preferences, not personality diagnoses.
    if re.search(
        r"\bI (?:do not|don't) like (?:being in )?(?:huge|large|big) crowds\b",
        text,
        re.IGNORECASE
    ):
        memories.append({
            "category": "preference",
            "key": "dislikes_large_crowds",
            "value": "dislikes large crowds",
            "operation": "remember"
        })

    match = re.search(
        r"\bI(?: am|'m|’m) really into\s+(.+?)(?:[.!?]|$)",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "preference",
            "key": "interests",
            "value": match.group(1).strip(),
            "operation": "remember"
        })

    # Explicit statement of the user's current project identity.
    # This does not capture future embodiment ambitions or hypothetical plans.
    match = re.search(
        r"\bI am developing ([A-Za-z][A-Za-z0-9_-]*) as my personal AI companion\b",
        text,
        re.IGNORECASE
    )

    if match:
        memories.append({
            "category": "project_fact",
            "key": match.group(1),
            "value": "personal AI companion",
            "operation": "remember"
        })

    return memories
