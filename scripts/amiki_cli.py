#!/usr/bin/env python3
"""Small Termux CLI for Amiki's local API; does not require the Android UI."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_URL = os.environ.get(
    "AMIKI_API_URL",
    "http://127.0.0.1:8000/api/v1",
).rstrip("/")
CHAT_TIMEOUT_SECONDS = 120.0
DEVICE_TIMEOUT_SECONDS = 75.0


def _api_url(path: str) -> str:
    parsed = urlsplit(BASE_URL)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError("AMIKI_API_URL must point to Amiki's local HTTP API.")
    return urljoin(BASE_URL + "/", path.lstrip("/"))


def request_json(
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    timeout: float = CHAT_TIMEOUT_SECONDS,
) -> dict:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        _api_url(path),
        data=body,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise RuntimeError("Amiki API response exceeded the CLI safety limit.")
    except HTTPError as error:
        detail = f"Amiki API returned HTTP {error.code}."
        try:
            body_json = json.loads(error.read(16_384).decode("utf-8"))
            if isinstance(body_json, dict) and isinstance(body_json.get("detail"), str):
                detail = body_json["detail"][:300]
        except (ValueError, UnicodeDecodeError, OSError):
            pass
        raise RuntimeError(detail) from None
    except URLError:
        raise RuntimeError("Amiki's local API is not reachable.") from None
    except TimeoutError:
        raise RuntimeError("Amiki's local API request timed out.") from None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("Amiki API returned invalid JSON.") from None
    if not isinstance(data, dict):
        raise RuntimeError("Amiki API returned an unexpected response shape.")
    return data


def ensure_server() -> None:
    """Start the existing Termux backend script if the API is not already healthy."""
    try:
        health = request_json("GET", "health", timeout=1.5)
        if health.get("status") == "ok":
            return
    except RuntimeError:
        pass

    start_script = Path(__file__).resolve().parent / "start_api.sh"
    if not start_script.is_file():
        raise RuntimeError("The backend start script is missing.")

    try:
        subprocess.run(
            ["bash", str(start_script)],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Amiki's backend could not be started from Termux.") from None

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            health = request_json("GET", "health", timeout=1.5)
            if health.get("status") == "ok":
                return
        except RuntimeError:
            pass
        time.sleep(0.4)

    raise RuntimeError(
        "Amiki's API did not become healthy. Check ~/amiki-api.log in Termux."
    )


def build_device_payload(action: str, values: list[str]) -> dict:
    if action == "open-url":
        if len(values) != 1:
            raise ValueError("Usage: amiki device-execute open-url https://example.com")
        return {"action": "open_url", "arguments": {"url": values[0]}}

    if action == "open-app":
        if len(values) != 1:
            raise ValueError("Usage: amiki device-execute open-app PACKAGE")
        return {"action": "open_app", "arguments": {"package": values[0]}}

    if action == "speak":
        text = " ".join(values).strip()
        if not text or len(text) > 4000:
            raise ValueError("Speech text must contain 1–4000 characters.")
        return {"action": "speak", "arguments": {"text": text}}

    if action == "set-alarm":
        if not values or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", values[0]):
            raise ValueError("Alarm time must use 24-hour HH:MM format.")
        hour_text, minute_text = values[0].split(":")
        arguments = {"hour": int(hour_text), "minute": int(minute_text)}
        label = " ".join(values[1:]).strip()
        if label:
            arguments["message"] = label[:160]
        return {"action": "set_alarm", "arguments": arguments}

    if action == "share-text":
        text = " ".join(values).strip()
        if not text or len(text) > 4000:
            raise ValueError("Shared text must contain 1–4000 characters.")
        return {"action": "share_text", "arguments": {"text": text}}

    if action == "call-contact":
        name = " ".join(values).strip()
        if not name or len(name) > 128:
            raise ValueError("Contact name must contain 1–128 characters.")
        return {"action": "call_contact", "arguments": {"name": name}}

    raise ValueError("Unsupported device action.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amiki",
        description="Use Amiki from Termux without opening the Amiki Android UI.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ask = commands.add_parser("ask", help="Send a natural-language request to Amiki.")
    ask.add_argument("message", nargs="+", help="Message to send to Amiki.")

    commands.add_parser("device-state", help="Query device state through Termux:API.")
    commands.add_parser("capabilities", help="Show backend and device capabilities.")

    execute = commands.add_parser(
        "device-execute",
        help="Request a device action; supported actions require an on-device confirmation.",
    )
    execute.add_argument(
        "action",
        choices=("open-url", "open-app", "speak", "set-alarm", "share-text", "call-contact"),
    )
    execute.add_argument("values", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ensure_server()
        if args.command == "ask":
            message = " ".join(args.message).strip()
            if not message or len(message) > 12_000:
                raise ValueError("Message must contain 1–12000 characters.")
            response = request_json(
                "POST",
                "chat",
                {"message": message, "request_id": uuid4().hex},
                timeout=CHAT_TIMEOUT_SECONDS,
            )
            print(response.get("response", "Amiki returned no response text."))
            return 0

        if args.command == "device-state":
            response = request_json("GET", "device/state", timeout=DEVICE_TIMEOUT_SECONDS)
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0 if response.get("status") in {"success", "partial"} else 1

        if args.command == "capabilities":
            response = request_json("GET", "device/capabilities")
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0

        payload = build_device_payload(args.action, args.values)
        response = request_json(
            "POST",
            "device/execute",
            payload,
            timeout=DEVICE_TIMEOUT_SECONDS,
        )
        print(json.dumps(response, ensure_ascii=False, indent=2))
        return 0 if response.get("status") == "accepted" else 1

    except (RuntimeError, ValueError, OSError) as error:
        print(f"Amiki: {str(error)[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
