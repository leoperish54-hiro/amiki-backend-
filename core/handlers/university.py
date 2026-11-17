"""University capability handler for Amiki."""


def handle(message):
    """Prepare a structured university capability result."""
    return {
        "instruction": "Help the user with their university-related request clearly, accurately, and step by step.",
        "message": message,
    }
