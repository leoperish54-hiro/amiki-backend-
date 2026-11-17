#!/usr/bin/env python3
"""Create a one-time UOSA first-owner setup code on this device.

Run once from the Amiki backend repository:
    .venv/bin/python scripts/create_uosa_setup_code.py

Copy the displayed code directly into the Amiki first-device enrollment flow.
It expires after 15 minutes and is deleted after successful enrollment. Never
commit the code, send it through chat, or place it in logs.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import uosa_security


def main() -> int:
    try:
        result = uosa_security.create_setup_code()
    except uosa_security.UosaError as exc:
        print(f"UOSA setup code was not created: {exc}", file=sys.stderr)
        return 1

    print("UOSA first-owner setup code (one use, expires in 15 minutes):")
    print(result["setup_code"])
    print(f"Protected local handoff file: {result['path']}")
    print("Copy it into the initial enrollment screen. Do not share or commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
