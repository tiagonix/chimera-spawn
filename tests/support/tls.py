"""Disposable TEST-ONLY TLS material generated with distro openssl.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TlsBundle:
    """Paths for a temporary CA, server identity, and client identity."""

    ca_cert: Path
    ca_key: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    other_client_cert: Path
    other_client_key: Path
    mismatch_cert: Path
    mismatch_key: Path


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


def _write_ext(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def generate_tls_bundle(directory: Path) -> TlsBundle:
    """Create a throwaway PKI for tests. Not for production use."""
    directory.mkdir(parents=True, exist_ok=True)
    ca_key = directory / "ca.key"
    ca_cert = directory / "ca.crt"
    server_key = directory / "server.key"
    server_csr = directory / "server.csr"
    server_cert = directory / "server.crt"
    client_key = directory / "client.key"
    client_csr = directory / "client.csr"
    client_cert = directory / "client.crt"
    other_ca_key = directory / "other-ca.key"
    other_ca_cert = directory / "other-ca.crt"
    other_client_key = directory / "other-client.key"
    other_client_csr = directory / "other-client.csr"
    other_client_cert = directory / "other-client.crt"
    mismatch_key = directory / "mismatch.key"
    mismatch_csr = directory / "mismatch.csr"
    mismatch_cert = directory / "mismatch.crt"
    ca_ext = directory / "ca.ext"
    other_ca_ext = directory / "other-ca.ext"
    server_ext = directory / "server.ext"
    client_ext = directory / "client.ext"
    mismatch_ext = directory / "mismatch.ext"
    _write_ext(
        ca_ext,
        "[v3]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n",
    )
    _write_ext(
        other_ca_ext,
        "[v3]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n",
    )
    _write_ext(
        server_ext,
        "[v3]\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n",
    )
    _write_ext(
        client_ext,
        "[v3]\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature\n"
        "extendedKeyUsage=clientAuth\n",
    )
    _write_ext(
        mismatch_ext,
        "[v3]\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:mismatch.example\n",
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=Chimera Test CA",
        "-keyout",
        str(ca_key),
        "-out",
        str(directory / "ca.csr"),
    )
    _openssl(
        "x509",
        "-req",
        "-sha256",
        "-in",
        str(directory / "ca.csr"),
        "-signkey",
        str(ca_key),
        "-out",
        str(ca_cert),
        "-days",
        "1",
        "-extfile",
        str(ca_ext),
        "-extensions",
        "v3",
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=Chimera Other CA",
        "-keyout",
        str(other_ca_key),
        "-out",
        str(directory / "other-ca.csr"),
    )
    _openssl(
        "x509",
        "-req",
        "-sha256",
        "-in",
        str(directory / "other-ca.csr"),
        "-signkey",
        str(other_ca_key),
        "-out",
        str(other_ca_cert),
        "-days",
        "1",
        "-extfile",
        str(other_ca_ext),
        "-extensions",
        "v3",
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=localhost",
        "-keyout",
        str(server_key),
        "-out",
        str(server_csr),
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(server_csr),
        "-CA",
        str(ca_cert),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(server_cert),
        "-days",
        "1",
        "-extfile",
        str(server_ext),
        "-extensions",
        "v3",
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=chimera-test-admin",
        "-keyout",
        str(client_key),
        "-out",
        str(client_csr),
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(client_csr),
        "-CA",
        str(ca_cert),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(client_cert),
        "-days",
        "1",
        "-extfile",
        str(client_ext),
        "-extensions",
        "v3",
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=wrong-admin",
        "-keyout",
        str(other_client_key),
        "-out",
        str(other_client_csr),
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(other_client_csr),
        "-CA",
        str(other_ca_cert),
        "-CAkey",
        str(other_ca_key),
        "-CAcreateserial",
        "-out",
        str(other_client_cert),
        "-days",
        "1",
        "-extfile",
        str(client_ext),
        "-extensions",
        "v3",
    )
    _openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=mismatch.example",
        "-keyout",
        str(mismatch_key),
        "-out",
        str(mismatch_csr),
    )
    _openssl(
        "x509",
        "-req",
        "-in",
        str(mismatch_csr),
        "-CA",
        str(ca_cert),
        "-CAkey",
        str(ca_key),
        "-CAcreateserial",
        "-out",
        str(mismatch_cert),
        "-days",
        "1",
        "-extfile",
        str(mismatch_ext),
        "-extensions",
        "v3",
    )
    for key in (server_key, client_key, other_client_key, mismatch_key, ca_key, other_ca_key):
        key.chmod(0o600)
    return TlsBundle(
        ca_cert=ca_cert,
        ca_key=ca_key,
        server_cert=server_cert,
        server_key=server_key,
        client_cert=client_cert,
        client_key=client_key,
        other_client_cert=other_client_cert,
        other_client_key=other_client_key,
        mismatch_cert=mismatch_cert,
        mismatch_key=mismatch_key,
    )
