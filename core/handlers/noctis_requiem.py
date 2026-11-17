"""Noctis Requiem capability handler for Amiki."""


def handle(message):
    """Prepare a structured Noctis Requiem capability result."""
    return {
        "instruction": "Use the user's Noctis Requiem project context and established canon when answering. Distinguish established canon from inference or creative suggestions, and do not invent project facts.",
        "message": message,
    }
