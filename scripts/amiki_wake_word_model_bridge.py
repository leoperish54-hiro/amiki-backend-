#!/usr/bin/env python3
"""Provision and serve Amiki's pinned Hey Amiki model over authenticated loopback HTTP.

This process never captures microphone audio and never performs inference. It only
downloads, verifies, caches, and serves the five pinned model assets to Amiki Android.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

MODEL_BASE_URL = (
    "https://huggingface.co/mobilebytesensei/"
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M/resolve/main/"
)
ASSETS = {
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx": (
        4000908,
        "7d5a25fbc14b423074fe0c58e1be388ccf3f1a815c649980232a0c47f81875e4",
    ),
    "decoder-epoch-12-avg-2-chunk-16-left-64.onnx": (
        1063189,
        "f61ebd3eed3773a44d088d53dfae92dbb6aec4839f4dcaee2d402414741663a3",
    ),
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx": (
        163380,
        "eae9da0c7e1e6c6a3f4cc42d167899c388f6c6701b94cb96320e4f55df79624c",
    ),
    "tokens.txt": (
        5006,
        "fd2ded4050a55d2b1578870ba8697d02371980217806b7558bd0a5cc60f3ba53",
    ),
    "bpe.model": (
        244837,
        "c8a2a0129c4ab8e463164c142f82d25649661b122c8cd0b7aab5c9e80b90ad24",
    ),
}
CHUNK_SIZE = 64 * 1024
MAX_ATTEMPTS = 3


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_asset(path: Path, name: str) -> bool:
    expected_size, expected_hash = ASSETS[name]
    try:
        return path.is_file() and path.stat().st_size == expected_size and (
            sha256_file(path) == expected_hash
        )
    except OSError:
        return False


def download_asset(name: str, destination: Path) -> None:
    expected_size, expected_hash = ASSETS[name]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if verify_asset(destination, name):
        return

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        temporary_name: str | None = None
        try:
            request = Request(
                MODEL_BASE_URL + name + "?download=true",
                headers={
                    "User-Agent": "Amiki-Termux-Model-Bridge/1.0",
                    "Accept": "application/octet-stream",
                },
            )
            digest = hashlib.sha256()
            received = 0
            with urlopen(request, timeout=90) as response:
                if response.status != 200:
                    raise OSError(f"Unexpected model host HTTP status {response.status}.")
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=str(destination.parent),
                    prefix=destination.name + ".", suffix=".part", delete=False,
                ) as output:
                    temporary_name = output.name
                    while received < expected_size:
                        chunk = response.read(min(CHUNK_SIZE, expected_size - received))
                        if not chunk:
                            break
                        received += len(chunk)
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())

            if received != expected_size:
                raise OSError(
                    f"Model asset {name} ended at {received} of {expected_size} bytes."
                )
            if not hmac.compare_digest(digest.hexdigest(), expected_hash):
                raise OSError(f"Model asset {name} failed its pinned SHA-256 check.")
            os.replace(temporary_name, destination)
            temporary_name = None
            return
        except (OSError, URLError, TimeoutError) as error:
            last_error = error
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
    raise RuntimeError(f"Could not provision pinned model asset {name}: {last_error}")


def provision_model(directory: Path) -> None:
    for name in ASSETS:
        download_asset(name, directory / name)


def make_handler(directory: Path, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AmikiModelBridge/1"
        sys_version = ""

        def log_message(self, _format: str, *_args) -> None:
            # Do not log request headers, authorization values, or query strings.
            return

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = "Bearer " + token
            return hmac.compare_digest(supplied, expected)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self._authorized():
                self._json(401, {"status": "unauthorized"})
                return
            if self.path != "/v1/shutdown":
                self._json(404, {"status": "not_found"})
                return
            self._json(200, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def do_GET(self) -> None:
            if not self._authorized():
                self._json(401, {"status": "unauthorized"})
                return
            if self.path == "/v1/health":
                self._json(200, {"status": "ok", "service": "amiki-model-bridge"})
                return
            if self.path == "/v1/manifest":
                self._json(200, {
                    "status": "ok",
                    "model": "sherpa-onnx-kws-zipformer-gigaspeech-3.3M",
                    "assets": [
                        {"name": name, "size": size, "sha256": digest}
                        for name, (size, digest) in ASSETS.items()
                    ],
                })
                return
            prefix = "/v1/assets/"
            if self.path.startswith(prefix):
                name = self.path[len(prefix):]
                if name not in ASSETS or "/" in name or "\\" in name:
                    self._json(404, {"status": "not_found"})
                    return
                path = directory / name
                if not verify_asset(path, name):
                    self._json(503, {"status": "asset_not_verified"})
                    return
                try:
                    size = path.stat().st_size
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with path.open("rb") as source:
                        while True:
                            chunk = source.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                return
            self._json(404, {"status": "not_found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True, help="Per-install random bearer token")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path.home() / ".local/share/amiki/hey-amiki-model",
    )
    args = parser.parse_args(argv)
    if len(args.token) < 32 or not all(c in "0123456789abcdefABCDEF" for c in args.token):
        parser.error("--token must be at least 32 hexadecimal characters")
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")

    try:
        provision_model(args.model_dir)
        server = ThreadingHTTPServer(
            ("127.0.0.1", args.port), make_handler(args.model_dir, args.token)
        )
    except Exception as error:
        print(f"Amiki model bridge startup failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(f"Amiki model bridge ready on 127.0.0.1:{args.port}; model verified.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
