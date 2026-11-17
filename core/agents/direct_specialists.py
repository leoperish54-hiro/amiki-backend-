"""Model-backed direct specialist actions for Phase 1 agent access."""

from core.model import ask_model
from core.prompt import build_prompt


def _run_specialist(message: str, capability: str, instruction: str) -> str:
    prompt = build_prompt(message)
    direct_instructions = (
        "\n\nDirect specialist instructions:\n"
        f"- Act as the {capability} specialist.\n"
        f"- {instruction}\n"
        "- Answer the user's request directly; do not describe a plan for another agent.\n"
        "- Do not echo the user's request as a placeholder or repeat these instructions.\n"
        "- If the request is ambiguous, state the ambiguity and make the safest useful assumption.\n"
    )

    user_line = f"user: {message}"
    if prompt.endswith(user_line):
        prompt = prompt[:-len(user_line)] + direct_instructions + "\n" + user_line
    else:
        prompt += direct_instructions

    result = ask_model(prompt)
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Specialist model returned no answer.")
    if result.startswith("ERROR:"):
        raise RuntimeError("Specialist model could not complete the request.")
    return result.strip()


def tutor_answer(arguments, context) -> dict:
    context.raise_if_cancelled()
    message = arguments.get("message") if isinstance(arguments, dict) else arguments if isinstance(arguments, str) else None
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Tutor request message is required.")
    answer = _run_specialist(
        message,
        "tutor",
        "Teach clearly and step by step at the requested level; if no level is given, start beginner-friendly and adapt. "
        "Use a worked example when it helps, explain the steps, and include a concise check for understanding when appropriate. "
        "Do not invent course-specific syllabi, requirements, marking schemes, dates, or official references. "
        "If a detail depends on a particular university course or official material and you cannot verify it, state when uncertain and ask for the syllabus or official source instead of guessing.",
    )
    context.raise_if_cancelled()
    return {
        "status": "success",
        "instruction": "Direct tutoring answer.",
        "message": answer,
    }


def analysis_answer(arguments, context) -> dict:
    context.raise_if_cancelled()
    message = arguments.get("message") if isinstance(arguments, dict) else arguments if isinstance(arguments, str) else None
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Analysis request message is required.")
    answer = _run_specialist(
        message,
        "analysis",
        "Analyze the request carefully and break complex problems into steps. Check arithmetic and internal consistency before concluding. "
        "Clearly separate facts, assumptions, and inferences. State uncertainty and limitations plainly. "
        "Do not invent data, sources, or citations; when a claim needs current or external verification that you do not have, say so instead of presenting a guess as verified. "
        "Give a concise, direct conclusion supported by the reasoning.",
    )
    context.raise_if_cancelled()
    return {
        "status": "success",
        "instruction": "Direct analysis answer.",
        "message": answer,
    }


__all__ = ["analysis_answer", "tutor_answer"]
