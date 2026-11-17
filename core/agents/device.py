"""Bounded device-agent planning and explicit action policies.

Read-only state can use Termux:API. A small allowlist of interactive actions can
run only after an on-device Termux confirmation; unsupported actions remain plans
for the Android client. Arbitrary shell execution is never exposed.
"""

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlsplit

from core.handlers.termux_device import termux_device_capabilities


@dataclass(frozen=True)
class DevicePlan:
    action: str
    arguments: dict[str, Any]
    requires_user_approval: bool
    reason: str


_ALLOWED_ACTIONS = {
    "open_url": True,
    "open_app": True,
    "speak": True,
    "share_text": True,
    "call_contact": True,
    "set_alarm": True,
    "battery_state": False,
    "device_state": False,
}

_ALLOWED_APPS = {
    "com.android.chrome",
    "com.google.android.apps.chrome",
    "com.termux",
    "com.android.settings",
}


def _parse_alarm_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(?i)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", value)
    if not match:
        raise ValueError("Alarm time must be like 18:00, 6:00 PM, or 6 PM.")

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()

    if minute > 59:
        raise ValueError("Alarm minutes must be between 00 and 59.")

    if meridiem:
        if hour < 1 or hour > 12:
            raise ValueError("12-hour alarm time must use an hour from 1 to 12.")
        if meridiem == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    elif hour > 23:
        raise ValueError("24-hour alarm time must use an hour from 00 to 23.")

    return hour, minute


def _normalize_directive(message: str) -> str:
    stripped = " ".join(message.strip().split())
    lowered = stripped.lower()
    for prefix in ("amiki:", "amiki,", "amiki "):
        if lowered.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


def plan_device_request(message: str) -> DevicePlan:
    """Translate a narrowly-scoped natural-language device command to a safe plan."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("A device command is required.")

    request = _normalize_directive(message)
    lowered = request.lower()

    if lowered in {"battery", "battery status", "check battery", "what is my battery", "what's my battery", "how much battery do i have"}:
        return plan_device_action("battery_state")

    if lowered in {
        "device state", "phone status", "device status", "check device state",
        "check my device", "what is my phone doing", "what's my phone status",
    }:
        return plan_device_action("device_state")

    if lowered in {"open settings", "open android settings", "settings"}:
        return plan_device_action("open_app", {"package": "com.android.settings"})

    if lowered in {"open chrome", "launch chrome", "start chrome"}:
        return plan_device_action("open_app", {"package": "com.android.chrome"})

    call_match = re.fullmatch(r"(?i)(?:please\s+)?(?:call|dial)\s+(.{1,128})", request)
    if call_match:
        contact = call_match.group(1).strip(" .!?")
        if contact:
            return plan_device_action("call_contact", {"name": contact})

    alarm_match = re.fullmatch(
        r"(?i)(?:please\s+)?(?:set|create)\s+(?:an?\s+)?alarm\s+(?:for\s+)?([^\s]+(?:\s+[ap]m)?)(?:\s+(?:to|for)\s+(.{1,160}))?",
        request,
    )
    if alarm_match:
        time_token = alarm_match.group(1).strip()
        label = (alarm_match.group(2) or "").strip()
        hour, minute = _parse_alarm_time(time_token)
        arguments = {"hour": hour, "minute": minute}
        if label:
            arguments["message"] = label
        return plan_device_action("set_alarm", arguments)

    url_match = re.fullmatch(
        r"(?i)(?:please\s+)?(?:open|visit|go to)\s+(https?://\S{1,2048})",
        request,
    )
    if url_match:
        return plan_device_action("open_url", {"url": url_match.group(1)})

    speak_match = re.fullmatch(
        r"(?i)(?:please\s+)?(?:speak|say|read aloud)\s+(.{1,4000})",
        request,
    )
    if speak_match:
        return plan_device_action("speak", {"text": speak_match.group(1)})

    share_match = re.fullmatch(
        r"(?i)(?:please\s+)?share text\s+(.{1,4000})",
        request,
    )
    if share_match:
        return plan_device_action("share_text", {"text": share_match.group(1)})

    raise ValueError("The message is not a supported device command.")


def plan_device_action(action: str, arguments: dict[str, Any] | None = None) -> DevicePlan:
    if not isinstance(action, str) or action not in _ALLOWED_ACTIONS:
        raise ValueError("Unsupported device action.")

    args = dict(arguments or {})

    if action == "open_url":
        url = args.get("url")
        if (
            not isinstance(url, str)
            or len(url) > 2048
            or any(ord(char) < 32 for char in url)
            or any(char.isspace() for char in url)
        ):
            raise ValueError("A valid HTTP(S) URL is required.")
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except ValueError:
            raise ValueError("A valid HTTP(S) URL is required.") from None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("A valid HTTP(S) URL without embedded credentials is required.")
        return DevicePlan(action, {"url": url}, True, "Open a user-requested URL after explicit device confirmation.")

    if action == "open_app":
        package = args.get("package")
        if package not in _ALLOWED_APPS:
            raise ValueError("The requested app is not on Amiki's safe allowlist.")
        return DevicePlan(action, {"package": package}, True, "Open an allowlisted Android application.")

    if action == "speak":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("Speech text must be non-empty and bounded.")
        return DevicePlan(action, {"text": text.strip()}, True, "Speak user-provided text through Amiki TTS.")

    if action == "share_text":
        text = args.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("Share text must be non-empty and bounded.")
        return DevicePlan(action, {"text": text.strip()}, True, "Open Android's user-facing share sheet.")

    if action == "call_contact":
        name = args.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError("Contact name must be non-empty and bounded.")
        return DevicePlan(action, {"name": name.strip()}, True, "Open the dialer for a user-selected contact.")

    if action == "set_alarm":
        try:
            hour = int(args.get("hour"))
            minute = int(args.get("minute"))
        except (TypeError, ValueError):
            raise ValueError("Alarm hour and minute must be integers.") from None

        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("Alarm time is outside the valid 24-hour range.")

        message = args.get("message", "")
        if not isinstance(message, str) or len(message) > 160:
            raise ValueError("Alarm message must be text no longer than 160 characters.")

        normalized = {"hour": hour, "minute": minute}
        if message.strip():
            normalized["message"] = message.strip()
        return DevicePlan(
            action,
            normalized,
            True,
            "Open Android's alarm UI after explicit device confirmation.",
        )

    return DevicePlan(action, {}, False, "Read-only device state request.")


def device_capabilities() -> dict[str, Any]:
    termux_capabilities = termux_device_capabilities()
    return {
        "status": "available",
        "actions": {
            "open_url": {"user_approval": True},
            "open_app": {"user_approval": True, "allowlist": sorted(_ALLOWED_APPS)},
            "speak": {"user_approval": True},
            "share_text": {"user_approval": True},
            "call_contact": {"user_approval": True},
            "set_alarm": {"user_approval": True},
            "battery_state": {"user_approval": False},
            "device_state": {"user_approval": False},
        },
        "execution_model": "plan_then_android_execute",
        "execution_modes": [
            "termux_api_with_user_confirmation",
            "android_client",
        ],
        "backend_read_only_execution": {
            "adapter": termux_capabilities.get("adapter", "unknown"),
            "requires_amiki_android_ui": termux_capabilities.get("requires_amiki_android_ui", False),
            "actions": termux_capabilities.get("actions", {}),
        },
        "backend_approved_execution": termux_capabilities.get("approved_actions", {}),
        "device_confirmation_available": termux_capabilities.get("confirmation_dialog_available", False),
    }


__all__ = [
    "DevicePlan",
    "device_capabilities",
    "plan_device_action",
    "plan_device_request",
]
