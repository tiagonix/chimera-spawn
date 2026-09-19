"""TLS helpers for Chimera Spawn remote transport.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import hashlib
import os
import ssl
import stat
from pathlib import Path
from typing import Any

from chimera.errors import ChimeraError
from chimera.models.config import ServerTlsConfig


def validate_tls_material(tls: ServerTlsConfig) -> tuple[str, str, str]:
    """Fail closed when remote TLS files are missing or unsafe.

    Returns the resolved certificate, private key, and client CA paths.
    """
    certificate = str(_require_readable_file(tls.certificate, "TLS certificate"))
    private_key = str(_require_private_key(tls.private_key))
    client_ca = str(_require_readable_file(tls.client_ca, "TLS client CA"))
    return certificate, private_key, client_ca


def build_server_ssl_context(tls: ServerTlsConfig) -> ssl.SSLContext:
    """Build a TLS server context that requires a client certificate."""
    certificate, private_key, client_ca = validate_tls_material(tls)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = False
    try:
        context.load_cert_chain(certificate, private_key)
        context.load_verify_locations(client_ca)
    except (OSError, ssl.SSLError) as error:
        raise ChimeraError(
            code="invalid_configuration",
            message="The server TLS configuration could not be loaded.",
            detail=str(error),
            suggestion=(
                "Install a server certificate, private key, and client CA, then restart "
                "chimera-server."
            ),
            status=500,
        ) from error
    return context


def resolve_client_tls_paths(
    ca_file: str, cert_file: str, key_file: str
) -> tuple[Path, Path, Path]:
    """Resolve and validate client TLS files without loading an SSL context."""
    return (
        _require_readable_file(ca_file, "TLS CA"),
        _require_readable_file(cert_file, "TLS client certificate"),
        _require_private_key(key_file, role="client"),
    )


def build_client_ssl_context(ca_file: str, cert_file: str, key_file: str) -> ssl.SSLContext:
    """Build a verified client context with mandatory mutual TLS."""
    ca_path, cert_path, key_path = resolve_client_tls_paths(ca_file, cert_file, key_file)
    try:
        context = ssl.create_default_context(cafile=str(ca_path))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_cert_chain(str(cert_path), str(key_path))
    except (OSError, ssl.SSLError) as error:
        raise ChimeraError(
            code="invalid_configuration",
            message="The client TLS configuration could not be loaded.",
            detail=str(error),
            suggestion="Check --tls-ca, --tls-cert, and --tls-key and retry.",
            status=400,
        ) from error
    return context


def certificate_sha256(der: bytes) -> str:
    """Return a hex SHA-256 fingerprint of a DER certificate."""
    return hashlib.sha256(der).hexdigest()


def certificate_subject(cert: dict[str, Any] | None) -> str:
    """Return a compact subject display string without dumping the certificate."""
    if not cert:
        return "unknown"
    aliases = {"commonName": "CN", "organizationName": "O", "organizationalUnitName": "OU"}
    subject = cert.get("subject") or ()
    parts: list[str] = []
    for relative in subject:
        for key, value in relative:
            parts.append(f"{aliases.get(str(key), key)}={value}")
    return ", ".join(parts) or "unknown"


def _require_readable_file(path_value: str, label: str) -> Path:
    """Require an existing readable file and return its resolved path string form."""
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        status = os.lstat(path)
    except FileNotFoundError as error:
        raise ChimeraError(
            code="invalid_configuration",
            message=f"{label} '{path}' does not exist.",
            suggestion=f"Create or install the {label.lower()} and retry.",
            status=400,
        ) from error
    except OSError as error:
        raise ChimeraError(
            code="invalid_configuration",
            message=f"{label} '{path}' cannot be inspected.",
            detail=str(error),
            status=400,
        ) from error
    if stat.S_ISLNK(status.st_mode):
        try:
            if not path.is_file():
                raise ChimeraError(
                    code="invalid_configuration",
                    message=f"{label} '{path}' is a symlink that does not resolve to a file.",
                    status=400,
                )
        except ChimeraError:
            raise
        except OSError as error:
            raise ChimeraError(
                code="invalid_configuration",
                message=f"{label} '{path}' cannot be read.",
                detail=str(error),
                status=400,
            ) from error
    elif not stat.S_ISREG(status.st_mode):
        raise ChimeraError(
            code="invalid_configuration",
            message=f"{label} '{path}' must be a regular file.",
            status=400,
        )
    if not os.access(path, os.R_OK):
        raise ChimeraError(
            code="invalid_configuration",
            message=f"{label} '{path}' is not readable.",
            status=400,
        )
    return path


def _require_private_key(path_value: str, *, role: str = "server") -> Path:
    """Refuse unexpected symlinks and overly permissive private keys."""
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        status = os.lstat(path)
    except FileNotFoundError as error:
        raise ChimeraError(
            code="invalid_configuration",
            message=f"TLS private key '{path}' does not exist.",
            suggestion="Install the private key as a regular file and retry.",
            status=400,
        ) from error
    except OSError as error:
        raise ChimeraError(
            code="invalid_configuration",
            message=f"TLS private key '{path}' cannot be inspected.",
            detail=str(error),
            status=400,
        ) from error
    if stat.S_ISLNK(status.st_mode):
        raise ChimeraError(
            code="invalid_configuration",
            message=f"TLS private key '{path}' must be a regular file, not a symlink.",
            suggestion="Copy the key into place as a regular file with mode 0600.",
            status=400,
        )
    if not stat.S_ISREG(status.st_mode):
        raise ChimeraError(
            code="invalid_configuration",
            message=f"TLS private key '{path}' must be a regular file.",
            status=400,
        )
    if status.st_mode & 0o077:
        raise ChimeraError(
            code="invalid_configuration",
            message=f"TLS private key '{path}' permissions are too open.",
            detail=f"Observed mode {stat.S_IMODE(status.st_mode):04o}; expected 0600.",
            suggestion="chmod 0600 the private key so only the owner can read it.",
            status=400,
        )
    if role == "client" and not os.access(path, os.R_OK):
        raise ChimeraError(
            code="invalid_configuration",
            message=f"TLS private key '{path}' is not readable.",
            status=400,
        )
    return path
