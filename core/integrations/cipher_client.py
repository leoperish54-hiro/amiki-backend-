"""Least-privilege client for the local Cipher defensive service."""
from __future__ import annotations
import ipaddress
import json
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class CipherIntegrationError(RuntimeError):
    """Safe, bounded error raised when Cipher cannot be reached or rejects a request."""


class CipherClient:
    MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(self, base_url: str, token: str, *, timeout: float = 3.0) -> None:
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Cipher token must contain at least 32 characters.")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 20:
            raise ValueError("timeout must be greater than 0 and at most 10 seconds.")
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Cipher URL must be a plain loopback HTTP origin.")
        host = parsed.hostname.lower()
        loopback = host == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                try:
                    addresses = socket.getaddrinfo(host, parsed.port or 80, type=socket.SOCK_STREAM)
                    loopback = bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_loopback for item in addresses)
                except (OSError, ValueError):
                    loopback = False
        if not loopback:
            raise ValueError("Cipher client may connect only to a loopback address.")
        authority_host = f"[{host}]" if ":" in host else host
        self.base_url = f"http://{authority_host}:{parsed.port or 8765}"
        self._token = token
        self._timeout = float(timeout)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if path not in {"/health", "/inspect", "/plan"}:
            raise ValueError("Unsupported Cipher endpoint.")
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers.update({"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"})
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
                if len(raw) > self.MAX_RESPONSE_BYTES:
                    raise CipherIntegrationError("Cipher response exceeded the size limit.")
                result = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 206 and path == "/inspect":
                try:
                    raw = exc.read(self.MAX_RESPONSE_BYTES + 1)
                    if len(raw) > self.MAX_RESPONSE_BYTES:
                        raise CipherIntegrationError("Cipher response exceeded the size limit.")
                    result = json.loads(raw.decode("utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    raise CipherIntegrationError("Cipher returned an invalid partial inspection response.") from None
            else:
                raise CipherIntegrationError(f"Cipher rejected the request (HTTP {exc.code}).") from None
        except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise CipherIntegrationError("Cipher is unavailable or returned an invalid response.") from None
        if not isinstance(result, dict):
            raise CipherIntegrationError("Cipher returned an invalid response contract.")
        return result

    def health(self) -> dict[str, Any]:
        result = self._request("GET", "/health")
        if result.get("service") != "cipher" or result.get("status") not in {"ok", "degraded"}:
            raise CipherIntegrationError("Cipher health response did not match the contract.")
        return result

    def inspect(self, root_id: str) -> dict[str, Any]:
        if not isinstance(root_id, str) or not root_id.strip() or len(root_id) > 64:
            raise ValueError("root_id must be a non-empty registered identifier.")
        result = self._request("POST", "/inspect", {"root_id": root_id})
        if result.get("root_id") != root_id or not isinstance(result.get("complete"), bool) or not isinstance(result.get("findings"), list) or result.get("detected_values_included") is not False:
            raise CipherIntegrationError("Cipher inspection response did not match the safe contract.")
        return result

    def plan(self, *, mode: str, resource_id: str, severity: str, persistent: bool = False, credential_compromised: bool = False, connected_resource_ids: list[str] | None = None) -> dict[str, Any]:
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("resource_id must be non-empty.")
        if not isinstance(persistent, bool) or not isinstance(credential_compromised, bool):
            raise ValueError("threat flags must be booleans.")
        result = self._request("POST", "/plan", {
            "mode": mode, "resource_id": resource_id, "severity": severity,
            "persistent": persistent, "credential_compromised": credential_compromised,
            "connected_resource_ids": connected_resource_ids or [],
        })
        if result.get("resource_id") != resource_id or result.get("execution_performed") is not False or result.get("external_operations_permitted") is not False or not isinstance(result.get("actions"), list):
            raise CipherIntegrationError("Cipher plan response violated the advisory-only contract.")
        return result


__all__ = ["CipherClient", "CipherIntegrationError"]
