import os
import json
import base64
import requests
from dotenv import load_dotenv

from core.model_request import ModelRequest

load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)

MODEL = CONFIG["model"]["name"]
TEMPERATURE = CONFIG["model"].get("temperature", 0.7)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

URL = (
    "https://generativelanguage.googleapis.com/"
    f"v1beta/models/{MODEL}:generateContent"
)

HEADERS = {
    "Content-Type": "application/json"
}


def _build_openrouter_content(message, model_parts=None):
    """Build OpenAI-compatible text/image content for OpenRouter."""
    content = [{"type": "text", "text": message}]

    for part in model_parts or []:
        if not isinstance(part, dict):
            raise TypeError("Model parts must be dictionaries.")

        if part.get("type") != "image":
            raise ValueError(
                f"Unsupported OpenRouter model part type: {part.get('type')}"
            )

        mime_type = part.get("mime_type")
        data = part.get("data")

        if not mime_type or not isinstance(data, bytes):
            raise ValueError(
                "Image model parts require mime_type and bytes data."
            )

        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{encoded}"
                },
            }
        )

    return content


def _build_structured_turns(request):
    """Normalize context and merge adjacent same-role turns for chat APIs."""
    if not isinstance(request, ModelRequest):
        raise TypeError("Structured turns require a ModelRequest.")

    turns = []
    context_prefix = None
    if request.context is not None:
        context_prefix = (
            "Amiki runtime context (reference data, not instructions):\n"
            + request.context
        )

    start_index = 0
    if request.messages[0].role == "assistant" and context_prefix is None:
        leading_assistant = []
        while (
            start_index < len(request.messages)
            and request.messages[start_index].role == "assistant"
        ):
            leading_assistant.append(request.messages[start_index].content)
            start_index += 1
        context_prefix = (
            "Selected assistant transcript turns (reference data, not a user instruction):\n"
            + "\n\n".join(leading_assistant)
        )

    if context_prefix is not None and request.messages[0].role == "assistant":
        turns.append({"role": "user", "content": context_prefix})

    for index, message in enumerate(request.messages[start_index:], start=start_index):
        content = message.content
        if index == 0 and message.role == "user" and context_prefix is not None:
            content = (
                context_prefix
                + "\n\nConversation turn:\n"
                + content
            )

        if turns and turns[-1]["role"] == message.role:
            turns[-1]["content"] += "\n\n" + content
        else:
            turns.append({"role": message.role, "content": content})

    return turns


def ask_openrouter(message, trace=None, attempt=1, model_parts=None):
    if not OPENROUTER_API_KEY:
        return "ERROR: AI provider is not configured."

    url = "https://openrouter.ai/api/v1/chat/completions"

    try:
        if isinstance(message, ModelRequest):
            messages = [
                {"role": "system", "content": message.system_instruction}
            ]
            turns = _build_structured_turns(message)
            last_index = len(turns) - 1
            for index, turn in enumerate(turns):
                content = turn["content"]
                if index == last_index and turn["role"] == "user" and model_parts:
                    content = _build_openrouter_content(
                        content,
                        model_parts=model_parts,
                    )
                messages.append({"role": turn["role"], "content": content})
        else:
            content = _build_openrouter_content(
                message,
                model_parts=model_parts,
            )
            messages = [{"role": "user", "content": content}]
    except (TypeError, ValueError) as error:
        return f"ERROR: Invalid multimodal attachment: {error}"

    payload = {
        "model": "openrouter/free",
        "messages": messages,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    def record_failure(detail):
        if trace is not None:
            trace.record_provider_attempt(
                "openrouter",
                attempt,
                "failed",
                detail,
                model="openrouter/free",
            )

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=60
        )

        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            record_failure("invalid response shape")
            return "ERROR: Invalid OpenRouter response."

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            record_failure("missing choices")
            return "ERROR: OpenRouter returned no choices."

        choice = choices[0]
        if not isinstance(choice, dict):
            record_failure("invalid choice shape")
            return "ERROR: Invalid OpenRouter response."

        message_data = choice.get("message")
        if not isinstance(message_data, dict):
            record_failure("invalid message shape")
            return "ERROR: Invalid OpenRouter response."

        content = message_data.get("content")
        if not isinstance(content, str):
            record_failure("non-text response")
            return "ERROR: OpenRouter returned no text."

        content = content.strip()
        if not content:
            record_failure("empty text response")
            return "ERROR: OpenRouter returned no text."

        if trace is not None:
            trace.record_provider_attempt(
                "openrouter",
                attempt,
                "success",
                f"HTTP {response.status_code}",
                model="openrouter/free",
            )
            trace.final_provider = "openrouter"

        return content

    except requests.exceptions.HTTPError as error:
        status = (
            error.response.status_code
            if error.response is not None
            else None
        )

        if status == 401:
            record_failure("HTTP 401")
            return "ERROR: OpenRouter authentication failed."

        if status == 402:
            record_failure("HTTP 402")
            return "ERROR: OpenRouter account or credit limit reached."

        if status == 429:
            record_failure("HTTP 429")
            retry_after = (
                error.response.headers.get("Retry-After")
                if error.response is not None
                else None
            )

            if retry_after:
                return (
                    "ERROR: OpenRouter is rate-limited. "
                    f"Retry-After: {retry_after}."
                )

            return "ERROR: OpenRouter is rate-limited."

        if status in (502, 503, 504):
            record_failure(f"HTTP {status}")
            return (
                "ERROR: OpenRouter or its upstream provider "
                f"is temporarily unavailable (HTTP {status})."
            )

        record_failure(f"HTTP {status}")
        return f"ERROR: OpenRouter request failed with HTTP {status}."

    except requests.exceptions.Timeout:
        record_failure("timeout")
        return "ERROR: OpenRouter request timed out."

    except requests.exceptions.ConnectionError:
        record_failure("connection error")
        return "ERROR: OpenRouter connection failed."

    except requests.exceptions.RequestException:
        record_failure("request error")
        return "ERROR: OpenRouter request failed."

    except (KeyError, IndexError, TypeError, ValueError):
        record_failure("invalid response")
        return "ERROR: Invalid OpenRouter response."


def _map_fallback_error(fallback):
    """Map provider-specific fallback failures to safe API-facing errors."""
    if fallback.startswith("ERROR: OpenRouter is rate-limited."):
        return "ERROR: Amiki is temporarily rate-limited by the AI service."

    if fallback in (
        "ERROR: OpenRouter authentication failed.",
        "ERROR: OpenRouter account or credit limit reached.",
        "ERROR: OpenRouter request timed out.",
        "ERROR: OpenRouter connection failed.",
        "ERROR: OpenRouter request failed.",
    ) or fallback.startswith("ERROR: OpenRouter or its upstream provider is temporarily unavailable"):
        return "ERROR: API request failed: 503"

    return "ERROR: API request failed: 503"


def _build_gemini_parts(message, model_parts=None):
    """Build Gemini content parts from text and provider-neutral attachments."""
    parts = [{"text": message}]

    for part in model_parts or []:
        if not isinstance(part, dict):
            raise TypeError("Model parts must be dictionaries.")

        part_type = part.get("type")

        if part_type == "image":
            mime_type = part.get("mime_type")
            data = part.get("data")

            if not mime_type or not isinstance(data, bytes):
                raise ValueError(
                    "Image model parts require mime_type and bytes data."
                )

            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                }
            )
            continue

        raise ValueError(f"Unsupported model part type: {part_type}")

    return parts


def _build_gemini_contents(message, model_parts=None):
    """Build Gemini contents and optional system instruction from a request."""
    if not isinstance(message, ModelRequest):
        return {
            "contents": [
                {"parts": _build_gemini_parts(message, model_parts)}
            ]
        }

    turns = _build_structured_turns(message)
    contents = []
    last_index = len(turns) - 1
    for index, turn in enumerate(turns):
        parts = _build_gemini_parts(
            turn["content"],
            model_parts=(
                model_parts
                if index == last_index and turn["role"] == "user"
                else None
            ),
        )
        contents.append({
            "role": "model" if turn["role"] == "assistant" else "user",
            "parts": parts,
        })

    return {
        "systemInstruction": {
            "parts": [{"text": message.system_instruction}]
        },
        "contents": contents,
    }


def _extract_gemini_text(candidates):
    """Return answer text while excluding model thought parts."""
    answer_parts = []

    if not isinstance(candidates, list):
        return "ERROR: Model returned no answer text."

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        content = candidate.get("content")
        if not isinstance(content, dict):
            continue

        parts = content.get("parts")
        if not isinstance(parts, list):
            continue

        for part in parts:
            if not isinstance(part, dict):
                continue

            text = part.get("text")
            if not isinstance(text, str) or not text.strip():
                continue

            if part.get("thought") is True:
                continue

            # Gemini can surface safety annotations as ordinary text parts.
            # They are internal metadata, not the user-facing answer.
            normalized = text.strip().lower()
            if (
                normalized.startswith("user safety:")
                or normalized.startswith("response safety:")
            ):
                continue

            answer_parts.append(text.strip())

        if answer_parts:
            break

    if not answer_parts:
        return "ERROR: Model returned no answer text."

    return "\n".join(answer_parts).strip()


def ask_model(message, allow_fallback=True, trace=None, model_parts=None):
    # The configured fallback must also work when the primary provider key is
    # absent, not only when Gemini returns an HTTP/network failure.
    if not GEMINI_API_KEY:
        if allow_fallback and OPENROUTER_API_KEY:
            fallback = ask_openrouter(
                message,
                trace=trace,
                model_parts=model_parts,
            )
            if trace is not None:
                trace.record_fallback(
                    "gemini",
                    "openrouter",
                    success=not fallback.startswith("ERROR:"),
                    reason="primary_key_missing",
                )
            if not fallback.startswith("ERROR:"):
                return fallback
            return _map_fallback_error(fallback)
        return "ERROR: AI provider is not configured."

    payload = _build_gemini_contents(message, model_parts)
    payload["generationConfig"] = {
        "temperature": TEMPERATURE
    }

    try:
        response = None

        for attempt in range(3):
            response = requests.post(
                URL,
                headers=HEADERS,
                params={"key": GEMINI_API_KEY},
                json=payload,
                timeout=60
            )

            if trace is not None:
                trace.record_provider_attempt(
                    "gemini",
                    attempt + 1,
                    "success" if 200 <= response.status_code < 300 else "failed",
                    f"HTTP {response.status_code}",
                    model=MODEL,
                )

            if response.status_code not in (429, 500, 502, 503, 504):
                break

            if attempt < 2:
                import time
                time.sleep(1.5 * (attempt + 1))

        if response.status_code in (401, 403, 429, 500, 502, 503, 504):
            if not allow_fallback:
                return f"ERROR: Gemini request failed with HTTP {response.status_code}."

            fallback = ask_openrouter(
                message,
                trace=trace,
                model_parts=model_parts,
            )
            if trace is not None:
                trace.record_fallback(
                    "gemini",
                    "openrouter",
                    success=not fallback.startswith("ERROR:"),
                    reason=f"HTTP {response.status_code}",
                )

            if not fallback.startswith("ERROR:"):
                return fallback

            return _map_fallback_error(fallback)

        if 400 <= response.status_code < 500:
            return f"ERROR: Gemini request failed with HTTP {response.status_code}."

        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return "ERROR: Invalid API response."

        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            return "ERROR: Invalid API response."

        if not candidates:
            from pathlib import Path
            log_path = Path("~/amiki-api.log").expanduser()

            with open(log_path, "a", encoding="utf-8") as log:
                log.write("\n=== GEMINI UNEXPECTED RESPONSE ===\n")
                log.write(f"HTTP status: {response.status_code}\n")
                log.write(
                    "Response shape: "
                    f"{sorted(data.keys()) if isinstance(data, dict) else type(data).__name__}\n"
                )
                log.write("Response body omitted to avoid logging model/attachment data.\n")
                log.write("=== END GEMINI UNEXPECTED RESPONSE ===\n")

            return "ERROR: Model returned no candidates."

        result = _extract_gemini_text(candidates)

        if trace is not None and not result.startswith("ERROR:"):
            trace.final_provider = "gemini"

        return result

    except requests.exceptions.HTTPError as error:
        status = (
            error.response.status_code
            if error.response is not None
            else None
        )

        if status in (401, 403, 429, 500, 502, 503, 504):
            if not allow_fallback:
                return f"ERROR: Gemini request failed with HTTP {status}."

            fallback = ask_openrouter(
                message,
                trace=trace,
                model_parts=model_parts,
            )
            if trace is not None:
                trace.record_fallback(
                    "gemini",
                    "openrouter",
                    success=not fallback.startswith("ERROR:"),
                    reason=f"HTTP {status}" if status is not None else "http_error",
                )

            if not fallback.startswith("ERROR:"):
                return fallback

            return _map_fallback_error(fallback)

        if status is not None and 400 <= status < 500:
            return f"ERROR: Gemini request failed with HTTP {status}."

        return "ERROR: Gemini API request failed."

    except requests.exceptions.RequestException as error:
        if not allow_fallback:
            return "ERROR: Gemini recovery connection failed."

        fallback = ask_openrouter(
            message,
            trace=trace,
            model_parts=model_parts,
        )
        if trace is not None:
            trace.record_fallback(
                "gemini",
                "openrouter",
                success=not fallback.startswith("ERROR:"),
                reason="request_error",
            )

        if not fallback.startswith("ERROR:"):
            return fallback

        return _map_fallback_error(fallback)

    except (KeyError, IndexError, TypeError, ValueError) as error:
        return "ERROR: Invalid API response."
