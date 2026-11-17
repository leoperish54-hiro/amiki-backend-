"""Analysis capability handler for Amiki."""


def handle(message):
    """Prepare a structured analysis capability result."""
    return {
        "instruction": "Analyze the user's request carefully and provide a clear, reasoned response.",
        "message": message,
    }
