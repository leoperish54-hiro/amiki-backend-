from __future__ import annotations

import hashlib
import io
import json
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import amiki_wake_word_model_bridge as bridge


def test_asset_manifest_contains_only_pinned_assets():
    assert len(bridge.ASSETS) == 5
    for name, (size, digest) in bridge.ASSETS.items():
        assert name and "/" not in name
        assert size > 0
        assert len(digest) == 64
        int(digest, 16)


def test_verify_asset_requires_exact_size_and_sha256(tmp_path):
    name = "sample"
    bridge.ASSETS[name] = (len(b"model"), hashlib.sha256(b"model").hexdigest())
    try:
        target = tmp_path / name
        target.write_bytes(b"model")
        assert bridge.verify_asset(target, name)
        target.write_bytes(b"bad")
        assert not bridge.verify_asset(target, name)
        target.write_bytes(b"model-extra")
        assert not bridge.verify_asset(target, name)
    finally:
        del bridge.ASSETS[name]


def test_http_bridge_requires_bearer_token_and_serves_manifest(tmp_path):
    token = "a" * 64
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), bridge.make_handler(tmp_path, token)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(base + "/v1/manifest")
        try:
            urllib.request.urlopen(request, timeout=2)
            assert False, "unauthenticated request must be rejected"
        except urllib.error.HTTPError as error:
            assert error.code == 401

        request = urllib.request.Request(
            base + "/v1/manifest",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read())
        assert payload["status"] == "ok"
        assert len(payload["assets"]) == 5
        assert {asset["name"] for asset in payload["assets"]} == set(bridge.ASSETS)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_bridge_serves_only_verified_assets(tmp_path):
    token = "b" * 64
    name = "tokens.txt"
    original = bridge.ASSETS[name]
    body = b"verified-token-file"
    bridge.ASSETS[name] = (len(body), hashlib.sha256(body).hexdigest())
    (tmp_path / name).write_bytes(body)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), bridge.make_handler(tmp_path, token)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/assets/{name}",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.read() == body

        (tmp_path / name).write_bytes(b"tampered")
        try:
            urllib.request.urlopen(request, timeout=2)
            assert False, "unverified asset must not be served"
        except urllib.error.HTTPError as error:
            assert error.code == 503
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        bridge.ASSETS[name] = original


def test_provision_model_reuses_valid_cached_assets(tmp_path, monkeypatch):
    calls = []
    original = dict(bridge.ASSETS)
    try:
        for name, (size, _digest) in original.items():
            payload = (name.encode("utf-8") * ((size // len(name)) + 1))[:size]
            # Use deterministic local fixtures without downloading model binaries.
            bridge.ASSETS[name] = (len(payload), hashlib.sha256(payload).hexdigest())
            (tmp_path / name).write_bytes(payload)

        def unexpected_network(_request, timeout=0):
            calls.append(timeout)
            raise AssertionError("valid cached model assets must not trigger a download")

        monkeypatch.setattr(bridge, "urlopen", unexpected_network)
        for name in bridge.ASSETS:
            bridge.download_asset(name, tmp_path / name)
        assert calls == []
    finally:
        bridge.ASSETS.clear()
        bridge.ASSETS.update(original)


def test_provision_model_enforces_one_shared_deadline(tmp_path, monkeypatch):
    network_calls = []

    def unexpected_network(_request, timeout=0):
        network_calls.append(timeout)
        raise AssertionError("expired provisioning must not start a network request")

    ticks = iter((100.0, 100.01, 100.02))
    monkeypatch.setattr(bridge.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(bridge, "urlopen", unexpected_network)
    with pytest.raises(RuntimeError, match="deadline"):
        bridge.provision_model(tmp_path, timeout_seconds=0.001)
    assert network_calls == []


def test_receive_token_from_authenticated_loopback_tcp():
    session = "c" * 64
    token = "d" * 64
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def send_token():
        connection, _address = listener.accept()
        with connection:
            received = b""
            while not received.endswith(b"\n"):
                chunk = connection.recv(1)
                if not chunk:
                    break
                received += chunk
            assert received == (session + "\n").encode("ascii")
            connection.sendall((token + "\n").encode("ascii"))

    sender = threading.Thread(target=send_token, daemon=True)
    sender.start()
    try:
        assert bridge.receive_token_from_tcp(listener.getsockname()[1], session) == token
    finally:
        listener.close()
        sender.join(timeout=2)


def test_receive_token_rejects_invalid_tcp_session_and_port():
    with pytest.raises(ValueError, match="session challenge"):
        bridge.receive_token_from_tcp(8765, "not-a-session")
    with pytest.raises(ValueError, match="handoff port"):
        bridge.receive_token_from_tcp(80, "e" * 64)


def test_download_asset_writes_and_atomically_publishes_verified_file(tmp_path, monkeypatch):
    name = "fixture-model.bin"
    payload = b"verified-model-payload"
    original = bridge.ASSETS.get(name)
    bridge.ASSETS[name] = (len(payload), hashlib.sha256(payload).hexdigest())

    class FakeResponse(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(_request, timeout=0):
        assert timeout > 0
        return FakeResponse(payload)

    monkeypatch.setattr(bridge, "urlopen", fake_urlopen)
    destination = tmp_path / name
    try:
        bridge.download_asset(name, destination)
        assert destination.read_bytes() == payload
        assert bridge.verify_asset(destination, name)
        assert list(tmp_path.glob("*.part")) == []
    finally:
        if original is None:
            bridge.ASSETS.pop(name, None)
        else:
            bridge.ASSETS[name] = original
