"""HTTP client for the hermes_bridge relay running on the user's Mac.

The relay listens on the Mac's loopback interface and is reached from the
VPS through an SSH reverse tunnel, so from Hermes' point of view it is a
plain ``http://127.0.0.1:<port>`` endpoint.

Authentication never puts the shared token on the wire: every request
carries an HMAC-SHA256 signature over method, path, timestamp, nonce and
body, and every response must carry the relay's signature over the same
nonce. Anything else listening on the port (the tunnel is down and another
local user grabbed it) therefore learns nothing reusable and is detected
instead of trusted. Proxy environment variables are ignored: the relay is
always reached directly.

Wire contract: ``integrations/transport-fever-3/PROTOCOL.md`` (section A).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

import httpx

PROTOCOL_VERSION = 1
DEFAULT_BRIDGE_URL = "http://127.0.0.1:47800"
_MAX_TIMEOUT_S = 120.0
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
# The relay refuses larger bodies before it can authenticate them, so its
# answer would be unsigned; refuse them here, where nothing has been sent.
_MAX_BODY_BYTES = 256 * 1024


class BridgeError(Exception):
    """A failure talking to the relay or reported by the mod.

    ``retryable`` follows the relay's own classification when the relay
    answered, and is True for transport failures where the request never
    reached the game (connection refused, tunnel down).
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.status is not None:
            out["http_status"] = self.status
        return out


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_signature(key: str, method: str, target: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Must match tf3_relay.request_signature byte for byte."""
    canonical = "\n".join(["tf3-v1", method.upper(), target, timestamp, nonce, _sha256_hex(body)])
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def response_signature(key: str, nonce: str, status: int, body: bytes) -> str:
    """Must match tf3_relay.response_signature byte for byte."""
    canonical = "\n".join(["tf3-v1-resp", nonce, str(int(status)), _sha256_hex(body)])
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _is_loopback_host(host: str) -> bool:
    if host in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_bridge_url(url: str, *, allow_remote: bool = False) -> str:
    """Return a normalized base URL or raise ValueError.

    Loopback http(s) is always allowed (the SSH tunnel provides transport
    security). A non-loopback host is allowed only over https and only when
    ``allow_remote`` is set, so a typo or a poisoned config cannot point the
    agent's game control at an arbitrary host.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"bridge_url must be an http(s) URL with a host, got {url!r}")
    if parts.username or parts.password:
        raise ValueError("bridge_url must not embed credentials")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("bridge_url must be just scheme://host:port")
    if not _is_loopback_host(parts.hostname):
        if not allow_remote:
            raise ValueError(
                f"bridge_url host {parts.hostname!r} is not loopback; the relay is meant to be "
                "reached through the SSH reverse tunnel at 127.0.0.1. Set "
                "plugins.entries.tf3.settings.allow_remote_bridge: true to override (https only)."
            )
        if parts.scheme != "https":
            raise ValueError("a non-loopback bridge_url must use https")
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class Screenshot:
    data: bytes
    mime: str
    width: Optional[int]
    height: Optional[int]
    source: Optional[str] = None


class BridgeClient:
    """Small synchronous client; one instance per tool call is fine."""

    def __init__(self, base_url: str, token: str, *, timeout_s: float = 30.0, allow_remote: bool = False,
                 transport: Optional[httpx.BaseTransport] = None):
        if not token:
            raise BridgeError("not_configured", "TF3_BRIDGE_TOKEN is not set")
        self.base_url = validate_bridge_url(base_url, allow_remote=allow_remote)
        self._token = token
        self.timeout_s = max(1.0, min(float(timeout_s), _MAX_TIMEOUT_S))
        self._transport = transport

    # -- low level ---------------------------------------------------------

    def _signed_headers(self, method: str, target: str, body: bytes) -> tuple:
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        headers = {
            "X-TF3-Protocol": str(PROTOCOL_VERSION),
            "X-TF3-Timestamp": timestamp,
            "X-TF3-Nonce": nonce,
            "X-TF3-Signature": request_signature(self._token, method, target, timestamp, nonce, body),
        }
        if body:
            headers["Content-Type"] = "application/json"
        return headers, nonce

    def _verify_response(self, resp: httpx.Response, nonce: str) -> None:
        presented = resp.headers.get("X-TF3-Response-Signature", "")
        expected = response_signature(self._token, nonce, resp.status_code, resp.content)
        if not presented or not hmac.compare_digest(presented.lower().encode("ascii", "replace"),
                                                    expected.encode("ascii")):
            raise BridgeError(
                "relay_unauthenticated",
                f"the service at {self.base_url} did not prove it is the TF3 relay (bad or missing response "
                f"signature on an HTTP {resp.status_code} answer). Either TF3_BRIDGE_TOKEN differs from the "
                "Mac's token, or something else is listening on that port while the SSH tunnel is down.",
                retryable=False,
                status=resp.status_code,
            )

    def _request(self, method: str, path: str, *, json_body: Any = None, params: Optional[dict] = None,
                 timeout_s: Optional[float] = None, retry_connect: bool = True) -> httpx.Response:
        timeout = self.timeout_s if timeout_s is None else max(1.0, min(float(timeout_s), _MAX_TIMEOUT_S))
        # Build the exact bytes and target we sign; httpx sends them unchanged.
        target = path + ("?" + urlencode(sorted(params.items())) if params else "")
        body = b"" if json_body is None else json.dumps(
            json_body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > _MAX_BODY_BYTES:
            raise BridgeError("body_too_large", f"request is {len(body)} bytes; the relay accepts at most "
                              f"{_MAX_BODY_BYTES}. Nothing was sent.")
        attempts = 2 if retry_connect else 1
        last_exc: Optional[Exception] = None
        for _ in range(attempts):
            headers, nonce = self._signed_headers(method, target, body)  # fresh nonce per attempt
            try:
                with httpx.Client(transport=self._transport, timeout=timeout, follow_redirects=False,
                                  trust_env=False) as http:
                    resp = http.request(method, self.base_url + target, content=body or None, headers=headers)
            except httpx.ConnectError as exc:
                # Nothing reached the relay, so one retry is always safe.
                last_exc = exc
                continue
            except httpx.TimeoutException as exc:
                raise BridgeError(
                    "timeout",
                    f"relay did not answer within {timeout:.0f}s ({type(exc).__name__})",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise BridgeError("transport_error", f"{type(exc).__name__}: {exc}", retryable=True) from exc
            self._verify_response(resp, nonce)
            return resp
        raise BridgeError(
            "bridge_unreachable",
            f"cannot reach the TF3 relay at {self.base_url} ({last_exc}). Is the Mac awake, the relay "
            "LaunchAgent running, and the SSH reverse tunnel up?",
            retryable=True,
        )

    @staticmethod
    def _raise_for_error(resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code >= 400 or not isinstance(body, dict) or body.get("ok") is False:
            err = (body or {}).get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                raise BridgeError(
                    str(err.get("code") or "relay_error"),
                    str(err.get("message") or f"HTTP {resp.status_code}"),
                    retryable=bool(err.get("retryable")),
                    status=resp.status_code,
                )
            raise BridgeError("relay_error", f"unexpected relay response (HTTP {resp.status_code})",
                              retryable=resp.status_code >= 500, status=resp.status_code)
        return body

    # -- public API --------------------------------------------------------

    def health(self) -> dict:
        resp = self._request("GET", "/v1/health", timeout_s=min(self.timeout_s, 10.0))
        return self._raise_for_error(resp)

    def call(self, op: str, args: Optional[dict] = None, *, timeout_s: Optional[float] = None,
             request_id: Optional[str] = None) -> dict:
        """Run one mod op and return its ``result`` object.

        ``request_id`` makes the call idempotent end to end: calling again with
        the same id after a timeout picks up the late result instead of
        queueing the op a second time.
        """
        wait_s = self.timeout_s if timeout_s is None else float(timeout_s)
        body = {
            "op": op,
            "args": args or {},
            "request_id": request_id or uuid.uuid4().hex,
            "timeout_s": max(1.0, min(wait_s, _MAX_TIMEOUT_S - 5.0)),
        }
        # Give the HTTP layer a little longer than the relay's own wait so the
        # relay's structured timeout error wins over a bare socket timeout.
        resp = self._request("POST", "/v1/call", json_body=body, timeout_s=body["timeout_s"] + 5.0)
        payload = self._raise_for_error(resp)
        result = payload.get("result")
        return result if isinstance(result, dict) else {"value": result}

    def screenshot(self, *, max_width: int = 1600, quality: int = 70, source: Optional[str] = None) -> Screenshot:
        params: dict = {"max_width": max(320, min(int(max_width), 3840)), "quality": max(30, min(int(quality), 95))}
        if source:
            params["source"] = source
        resp = self._request("GET", "/v1/screenshot", params=params, timeout_s=45.0)
        mime = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if resp.status_code != 200 or not mime.startswith("image/"):
            self._raise_for_error(resp)
            raise BridgeError("relay_error", "relay returned no image")
        if len(resp.content) > _MAX_SCREENSHOT_BYTES:
            raise BridgeError("screenshot_too_large", f"screenshot is {len(resp.content)} bytes; lower max_width")

        def _int_header(name: str) -> Optional[int]:
            try:
                return int(resp.headers.get(name, ""))
            except ValueError:
                return None

        return Screenshot(resp.content, mime, _int_header("X-Image-Width"), _int_header("X-Image-Height"),
                          resp.headers.get("X-Image-Source"))
