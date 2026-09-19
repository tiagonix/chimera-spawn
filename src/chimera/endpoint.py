"""Remote server endpoint parsing for chimeractl.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

from chimera.errors import ChimeraError

DEFAULT_REMOTE_PORT = 8080
STREAM_TERM_MAX_LENGTH = 64
# fullmatch() already requires the whole string; \z is invalid before Python 3.14.
_STREAM_TERM_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}")


def parse_remote_endpoint(value: str) -> tuple[str, int]:
    """Parse HOST, HOST:PORT, or [IPv6]:PORT into a hostname and port.

    Schemes, paths, and query strings are rejected. The caller always
    constructs https:// and wss:// URLs from the returned values.
    """
    text = value.strip()
    if not text:
        raise ChimeraError(
            code="invalid_argument",
            message="A remote server host is required.",
            suggestion="Pass --host HOST or --host HOST:PORT.",
            status=400,
        )
    lowered = text.lower()
    if "://" in text or lowered.startswith(("http:", "https:", "ws:", "wss:")):
        raise ChimeraError(
            code="invalid_argument",
            message="Remote --host must be HOST or HOST:PORT, not a URL.",
            detail=f"Unsupported endpoint syntax: {text}",
            suggestion="Example: --host server.example.net:8080",
            status=400,
        )
    if "/" in text or "?" in text or "#" in text or "@" in text:
        raise ChimeraError(
            code="invalid_argument",
            message="Remote --host cannot include a path, query, or userinfo.",
            suggestion="Example: --host server.example.net:8080",
            status=400,
        )
    if text.startswith("["):
        closing = text.find("]")
        if closing <= 1:
            raise ChimeraError(
                code="invalid_argument",
                message="IPv6 --host values must be written as [address] or [address]:PORT.",
                status=400,
            )
        host = text[1:closing]
        rest = text[closing + 1 :]
        if not host:
            raise ChimeraError(
                code="invalid_argument",
                message="IPv6 --host values must include an address inside the brackets.",
                status=400,
            )
        try:
            ipaddress.IPv6Address(host)
        except ValueError as error:
            raise ChimeraError(
                code="invalid_argument",
                message="IPv6 --host values must contain a valid IPv6 address.",
                detail=str(error),
                suggestion="Write the address as [IPv6] or [IPv6]:PORT.",
                status=400,
            ) from error
        if not rest:
            return host, DEFAULT_REMOTE_PORT
        if not rest.startswith(":"):
            raise ChimeraError(
                code="invalid_argument",
                message="IPv6 --host values must be written as [address] or [address]:PORT.",
                status=400,
            )
        return host, _parse_port(rest[1:])
    if text.count(":") > 1:
        raise ChimeraError(
            code="invalid_argument",
            message="Unbracketed IPv6 --host values are ambiguous.",
            suggestion="Write the address as [IPv6] or [IPv6]:PORT.",
            status=400,
        )
    if ":" in text:
        host, port_text = text.rsplit(":", 1)
        if not host:
            raise ChimeraError(
                code="invalid_argument",
                message="Remote --host must include a hostname or address before the port.",
                status=400,
            )
        return host, _parse_port(port_text)
    return text, DEFAULT_REMOTE_PORT


def remote_authority(host: str, port: int) -> str:
    """Return the URL authority for an HTTPS/WSS endpoint."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def remote_http_url(host: str, port: int) -> str:
    """Return the HTTPS origin for a remote server."""
    return f"https://{remote_authority(host, port)}"


def remote_ws_url(host: str, port: int) -> str:
    """Return the WSS origin for a remote server."""
    return f"wss://{remote_authority(host, port)}"


def _parse_port(value: str) -> int:
    """Validate a TCP port in the 1..65535 range."""
    if not value.isdigit():
        raise ChimeraError(
            code="invalid_argument",
            message="Remote --host port must be an integer between 1 and 65535.",
            detail=f"Invalid port: {value}",
            status=400,
        )
    port = int(value)
    if port < 1 or port > 65535:
        raise ChimeraError(
            code="invalid_argument",
            message="Remote --host port must be an integer between 1 and 65535.",
            detail=f"Invalid port: {value}",
            status=400,
        )
    return port


def is_valid_stream_term(value: str) -> bool:
    """Return True when value is a conservative TERM name, not free-form text."""
    if not value or len(value) > STREAM_TERM_MAX_LENGTH:
        return False
    return bool(_STREAM_TERM_RE.fullmatch(value))


def select_client_term(raw: str | None, *, is_tty: bool) -> str | None:
    """Return the TERM to send on a TTY start frame, or None to omit it.

    Missing or invalid names are omitted so a console still opens. Pipe and
    file sessions never send TERM even when the process environment has one.
    """
    if not is_tty or raw is None:
        return None
    if not is_valid_stream_term(raw):
        return None
    return raw


def parse_stream_control(payload: str) -> dict[str, Any] | None:
    """Parse a WebSocket text control message, or return None if it is not control."""
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if not isinstance(kind, str) or not kind:
        return None
    return message


def encode_stream_control(message: dict[str, Any]) -> str:
    """Serialize a stream control message without extra whitespace."""
    return json.dumps(message, separators=(",", ":"), sort_keys=True)
