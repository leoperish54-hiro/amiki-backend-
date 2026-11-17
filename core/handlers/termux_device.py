"""Bounded Termux:API device queries and confirmation-gated actions.

Commands are selected from fixed allowlists and passed as argument arrays. This
module never invokes a shell or executes arbitrary commands.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

_COMMANDS = {
    "battery_state": "termux-battery-status",
}
_TIMEOUT_SECONDS = 3.0
_MAX_OUTPUT_CHARS = 8192


def _am_executable() -> str | None:
    return shutil.which("am") or (
        "/system/bin/am" if os.path.isfile("/system/bin/am") else None
    )


def termux_device_capabilities() -> dict[str, Any]:
    """Report detected read-only and confirmation-gated Termux execution paths."""
    available = {
        action: bool(shutil.which(command))
        for action, command in _COMMANDS.items()
    }
    dialog_available = bool(shutil.which("termux-dialog"))
    am_available = bool(_am_executable())
    approved_actions = {
        "open_url": bool(shutil.which("termux-open-url")) and dialog_available,
        "open_app": am_available and dialog_available,
        "speak": bool(shutil.which("termux-tts-speak")) and dialog_available,
        "set_alarm": am_available and dialog_available,
        "share_text": bool(shutil.which("termux-share")) and dialog_available,
        "call_contact": bool(shutil.which("termux-contact-list")) and am_available and dialog_available,
    }
    return {
        "adapter": "termux_api",
        "requires_amiki_android_ui": False,
        "actions": {
            action: {
                "available": is_available,
                "read_only": True,
                "requires_amiki_android_ui": False,
            }
            for action, is_available in available.items()
        },
        "approved_actions": {
            action: {
                "available": is_available,
                "requires_user_confirmation": True,
                "requires_amiki_android_ui": False,
            }
            for action, is_available in approved_actions.items()
        },
        "confirmation_dialog_available": dialog_available,
    }


def read_device_action(action: str) -> dict[str, Any]:
    """Execute one fixed read-only device query and return a bounded result."""
    if action == "device_state":
        capabilities = termux_device_capabilities()
        battery = read_device_action("battery_state")
        status = "success" if battery.get("status") == "success" else "partial"
        result = {
            "status": status,
            "action": "device_state",
            "source": "termux_api",
            "requires_amiki_android_ui": False,
            "capabilities": capabilities,
            "limitations": [
                "This is an on-demand read-only query, not a persistent device bridge heartbeat.",
                "Interactive actions use a separate confirmation-gated path when the required Termux commands are available.",
            ],
        }
        if battery.get("status") == "success":
            result["battery"] = battery.get("battery")
        else:
            result["battery_status"] = battery.get("status", "unknown")
            result["reason"] = battery.get(
                "reason",
                "Battery state could not be observed through Termux:API.",
            )
        return result

    if action not in _COMMANDS:
        return {
            "status": "unsupported",
            "action": action if isinstance(action, str) else "unknown",
            "reason": "This read-only Termux device action is not supported.",
        }

    command_name = _COMMANDS[action]
    executable = shutil.which(command_name)
    if not executable:
        return {
            "status": "unavailable",
            "action": action,
            "source": "termux_api",
            "requires_amiki_android_ui": False,
            "reason": (
                "Termux:API is unavailable. Install the Termux:API Android app "
                "and the termux-api package in Termux, then retry."
            ),
        }

    try:
        completed = subprocess.run(
            [executable],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "action": action,
            "source": "termux_api",
            "reason": "The device query timed out.",
        }
    except OSError:
        return {
            "status": "unavailable",
            "action": action,
            "source": "termux_api",
            "reason": "The Termux:API command could not be started.",
        }

    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    if len(stdout) > _MAX_OUTPUT_CHARS:
        return {
            "status": "error",
            "action": action,
            "source": "termux_api",
            "reason": "The device query returned more data than permitted.",
        }
    if completed.returncode != 0:
        return {
            "status": "error",
            "action": action,
            "source": "termux_api",
            "reason": "Termux:API could not complete the device query.",
        }

    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "action": action,
            "source": "termux_api",
            "reason": "Termux:API returned an invalid response.",
        }

    if not isinstance(payload, dict):
        return {
            "status": "error",
            "action": action,
            "source": "termux_api",
            "reason": "Termux:API returned an unexpected response shape.",
        }

    percentage = payload.get("percentage")
    if (
        not isinstance(percentage, (int, float))
        or isinstance(percentage, bool)
        or not 0 <= percentage <= 100
    ):
        return {
            "status": "error",
            "action": action,
            "source": "termux_api",
            "reason": "Termux:API did not return a valid battery percentage.",
        }

    battery = {"percentage": int(percentage)}

    # Explicit allowlist: do not expose arbitrary Termux:API fields.
    for key in ("status", "plugged", "health", "technology"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            battery[key] = value.strip()[:40]

    numeric_fields = (
        ("temperature", "temperature_c", -50, 150),
        ("voltage", "voltage_mv", 0, 10000),
        ("current", "current_ua", -10000000, 10000000),
        ("current_average", "current_average_ua", -10000000, 10000000),
        ("level", "level", 0, 10000),
        ("scale", "scale", 0, 10000),
        ("charge_counter", "charge_counter_uah", 0, 100000000),
    )
    for source_key, target_key, minimum, maximum in numeric_fields:
        value = payload.get(source_key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and minimum <= value <= maximum
        ):
            battery[target_key] = value

    present = payload.get("present")
    if isinstance(present, bool):
        battery["present"] = present

    return {
        "status": "success",
        "action": action,
        "source": "termux_api",
        "requires_amiki_android_ui": False,
        "battery": battery,
    }


def _confirmation_text(action: str, arguments: dict[str, Any]) -> str:
    if action == "open_url":
        return "Open this URL? " + str(arguments.get("url", ""))[:300]
    if action == "open_app":
        return "Open the allowlisted app " + str(arguments.get("package", "")) + "?"
    if action == "speak":
        return "Speak this text aloud? " + str(arguments.get("text", ""))[:180]
    if action == "set_alarm":
        label = str(arguments.get("message", ""))
        return (
            "Open the Android alarm screen for "
            + f"{int(arguments['hour']):02d}:{int(arguments['minute']):02d}"
            + (f" ({label[:80]})" if label else "")
            + "?"
        )
    if action == "share_text":
        return "Share this text with another app? " + str(arguments.get("text", ""))[:180]
    if action == "call_contact":
        return (
            "Find contact "
            + repr(str(arguments.get("name", ""))[:128])
            + " and open the dialer? Amiki will not place the call."
        )
    return "Allow this Amiki device action?"


def execute_approved_device_action(
    action: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask for on-device confirmation, then execute only a supported fixed action."""
    from core.agents.device import plan_device_action

    try:
        plan = plan_device_action(action, arguments or {})
    except (TypeError, ValueError) as exc:
        return {
            "status": "rejected",
            "action": action if isinstance(action, str) else "unknown",
            "reason": str(exc),
        }

    supported = {"open_url", "open_app", "speak", "set_alarm", "share_text", "call_contact"}
    if action not in supported:
        return {
            "status": "unsupported",
            "action": action,
            "reason": "This action still requires the Amiki Android client.",
            "execution_model": "android_client",
        }
    if not plan.requires_user_approval:
        return {
            "status": "rejected",
            "action": action,
            "reason": "This action has no explicit confirmation requirement.",
        }

    if action == "open_url":
        preflight_executable = shutil.which("termux-open-url")
    elif action == "speak":
        preflight_executable = shutil.which("termux-tts-speak")
    elif action == "share_text":
        preflight_executable = shutil.which("termux-share")
    elif action == "call_contact":
        preflight_executable = (
            shutil.which("termux-contact-list")
            if _am_executable()
            else None
        )
    else:
        preflight_executable = _am_executable()
    if not preflight_executable:
        return {
            "status": "unavailable",
            "action": action,
            "reason": "The required Termux/Android command is unavailable; no action was executed.",
        }

    dialog = shutil.which("termux-dialog")
    if not dialog:
        return {
            "status": "unavailable",
            "action": action,
            "reason": "Termux:API confirmation dialog is unavailable; no action was executed.",
        }

    prompt = _confirmation_text(action, plan.arguments)
    try:
        confirmation = subprocess.run(
            [dialog, "confirm", "-t", "Amiki device action", "-i", prompt],
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "action": action,
            "reason": "Device confirmation timed out; no action was executed.",
        }
    except OSError:
        return {
            "status": "unavailable",
            "action": action,
            "reason": "The Termux:API confirmation dialog could not be started.",
        }

    if confirmation.returncode != 0 or len(confirmation.stdout or "") > 4096:
        return {
            "status": "error",
            "action": action,
            "reason": "Device confirmation did not return a valid result; no action was executed.",
        }
    try:
        confirmation_payload = json.loads(confirmation.stdout or "")
    except (TypeError, ValueError):
        confirmation_payload = None
    if (
        not isinstance(confirmation_payload, dict)
        or confirmation_payload.get("code") != 0
        or confirmation_payload.get("error")
    ):
        return {
            "status": "cancelled",
            "action": action,
            "reason": "The device action was not approved; no action was executed.",
        }

    executable: str | None
    command: list[str]
    command_input: str | None = None
    if action == "open_url":
        executable = shutil.which("termux-open-url")
        command = [executable, plan.arguments["url"]] if executable else []
    elif action == "speak":
        executable = shutil.which("termux-tts-speak")
        command = [executable, "--", plan.arguments["text"]] if executable else []
    elif action == "share_text":
        executable = shutil.which("termux-share")
        command = (
            [executable, "-a", "send", "-c", "text/plain", "-t", "Shared by Amiki"]
            if executable else []
        )
        command_input = plan.arguments["text"]
    elif action == "call_contact":
        contact_executable = shutil.which("termux-contact-list")
        executable = _am_executable()
        if not contact_executable or not executable:
            return {
                "status": "unavailable",
                "action": action,
                "reason": "Contact lookup or dialer support is unavailable; no call was placed.",
            }
        try:
            contacts_result = subprocess.run(
                [contact_executable],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {
                "status": "error",
                "action": action,
                "reason": "Contact lookup failed; no call was placed.",
            }
        contacts_output = contacts_result.stdout or ""
        if contacts_result.returncode != 0 or len(contacts_output) > 100_000:
            return {
                "status": "error",
                "action": action,
                "reason": "Contact lookup was denied or exceeded its safety limit; no call was placed.",
            }
        try:
            contacts_payload = json.loads(contacts_output)
        except (TypeError, ValueError):
            contacts_payload = None
        if not isinstance(contacts_payload, list):
            return {
                "status": "error",
                "action": action,
                "reason": "Contact lookup returned an invalid response; no call was placed.",
            }
        requested_name = plan.arguments["name"].casefold()
        valid_contacts = [
            item for item in contacts_payload
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        matches = [
            item for item in valid_contacts
            if item["name"].strip().casefold() == requested_name
        ]
        if not matches:
            matches = [
                item for item in valid_contacts
                if item["name"].strip().casefold().startswith(requested_name)
            ]
        if len(matches) != 1:
            reason = (
                "No matching contact was found; no call was placed."
                if not matches
                else "Multiple contacts match; specify the full contact name. No call was placed."
            )
            return {"status": "error", "action": action, "reason": reason}
        raw_number = matches[0].get("number") or matches[0].get("phone")
        if not isinstance(raw_number, str):
            return {
                "status": "error",
                "action": action,
                "reason": "The matching contact has no usable phone number; no call was placed.",
            }
        phone_number = re.sub(r"[\s().-]", "", raw_number)
        if not re.fullmatch(r"\+?\d{3,20}", phone_number):
            return {
                "status": "error",
                "action": action,
                "reason": "The matching contact's phone number is invalid; no call was placed.",
            }
        command = [
            executable,
            "start",
            "-a", "android.intent.action.DIAL",
            "-d", "tel:" + phone_number,
        ]
    else:
        executable = _am_executable()
        command = [executable, "start"] if executable else []
        if executable and action == "open_app":
            command.extend([
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                "-p", plan.arguments["package"],
            ])
        elif executable and action == "set_alarm":
            command.extend([
                "-a", "android.intent.action.SET_ALARM",
                "--ei", "android.intent.extra.alarm.HOUR", str(plan.arguments["hour"]),
                "--ei", "android.intent.extra.alarm.MINUTES", str(plan.arguments["minute"]),
                "--ez", "android.intent.extra.alarm.SKIP_UI", "false",
            ])
            if plan.arguments.get("message"):
                command.extend([
                    "--es", "android.intent.extra.alarm.MESSAGE",
                    plan.arguments["message"],
                ])

    if not executable:
        return {
            "status": "unavailable",
            "action": action,
            "reason": "The required Termux/Android command is unavailable; no action was executed.",
        }

    try:
        completed = subprocess.run(
            command,
            input=command_input,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "action": action,
            "reason": "The device command timed out.",
        }
    except OSError:
        return {
            "status": "error",
            "action": action,
            "reason": "The device command could not be started.",
        }

    output = ((completed.stdout or "") + "\n" + (completed.stderr or ""))[:_MAX_OUTPUT_CHARS]
    if completed.returncode != 0 or "Error:" in output or "No activity found" in output:
        return {
            "status": "error",
            "action": action,
            "reason": "Android did not accept the device action.",
        }

    messages = {
        "open_url": "Android accepted the request to open the URL.",
        "open_app": "Android accepted the request to launch the app.",
        "speak": "Termux:API accepted the text-to-speech request.",
        "set_alarm": "Android accepted the request to open the alarm screen; verify and save the alarm in Clock.",
        "share_text": "Android accepted the request to open the text-sharing chooser.",
        "call_contact": "Android accepted the request to open the dialer; no call was placed.",
    }
    return {
        "status": "accepted",
        "action": action,
        "reason": messages[action],
        "execution_model": "termux_api_with_user_confirmation",
        "confirmation": "approved",
        "verification": "command_accepted; final on-device outcome is not independently verified",
        "requires_amiki_android_ui": False,
    }


__all__ = [
    "execute_approved_device_action",
    "read_device_action",
    "termux_device_capabilities",
]
