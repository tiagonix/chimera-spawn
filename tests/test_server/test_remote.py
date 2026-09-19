"""Mutual TLS remote transport tests using disposable openssl certificates."""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from chimera.cli.client import ChimeraClient, ClientError
from chimera.models.config import ServerTlsConfig
from chimera.server.api import ApiServer
from chimera.server.service import PeerCredentials
from chimera.tls import build_server_ssl_context
from tests.support.tls import generate_tls_bundle


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def tls_bundle(tmp_path):
    return generate_tls_bundle(tmp_path / "pki")


@pytest.fixture
def service():
    command_service = Mock()
    command_service.execute = AsyncMock(
        return_value={"server": {"running": True}, "containers": {}}
    )
    command_service.authorize = Mock()
    command_service.state_engine = Mock()
    command_service.state_engine.validate_stream_target = AsyncMock()
    return command_service


async def _start_tls_server(tmp_path, service, bundle, *, server_cert=None, server_key=None):
    port = _free_port()
    tls = ServerTlsConfig(
        certificate=str(server_cert or bundle.server_cert),
        private_key=str(server_key or bundle.server_key),
        client_ca=str(bundle.ca_cert),
    )
    server = ApiServer(
        tmp_path / "server.sock",
        service,
        "chimera-admin",
        remote_host="127.0.0.1",
        remote_port=port,
        ssl_context=build_server_ssl_context(tls),
    )
    await server.start()
    return server, port


def _remote_client(bundle, port: int, *, host: str = "localhost") -> ChimeraClient:
    return ChimeraClient(
        host=f"{host}:{port}",
        timeout=5,
        tls_ca=str(bundle.ca_cert),
        tls_cert=str(bundle.client_cert),
        tls_key=str(bundle.client_key),
    )


@pytest.mark.asyncio
async def test_tls_listener_accepts_verified_client(service, tmp_path, tls_bundle):
    """A complete TLS configuration serves the same command API as Unix."""
    server, port = await _start_tls_server(tmp_path, service, tls_bundle)
    try:
        result = await asyncio.to_thread(_remote_client(tls_bundle, port).request, "status", {})
        unix = await asyncio.to_thread(
            ChimeraClient(socket_path=str(tmp_path / "server.sock")).request, "status", {}
        )
        assert result == unix
        assert service.execute.await_count == 2
        first_peer = service.execute.await_args_list[0].args[2]
        second_peer = service.execute.await_args_list[1].args[2]
        assert first_peer.transport == "tls"
        assert first_peer.cert_sha256
        assert "CN=chimera-test-admin" in (first_peer.cert_subject or "")
        assert second_peer.transport == "unix"
        assert second_peer.uid is not None
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_tls_rejects_client_without_certificate(service, tmp_path, tls_bundle):
    """Anonymous HTTPS clients cannot call the remote API."""
    server, port = await _start_tls_server(tmp_path, service, tls_bundle)
    try:

        def attempt() -> None:
            import ssl as sslmod

            context = sslmod.create_default_context(cafile=str(tls_bundle.ca_cert))
            with httpx.Client(
                base_url=f"https://localhost:{port}", verify=context, timeout=5
            ) as client:
                client.post("/api/v1/command", json={"command": "status", "args": {}})

        with pytest.raises(httpx.RequestError):
            await asyncio.to_thread(attempt)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_hostname_mismatch_fails_closed(service, tmp_path, tls_bundle):
    """Certificate SAN rules apply to --host; verification is not disabled."""
    server, port = await _start_tls_server(
        tmp_path,
        service,
        tls_bundle,
        server_cert=tls_bundle.mismatch_cert,
        server_key=tls_bundle.mismatch_key,
    )
    try:
        client = _remote_client(tls_bundle, port, host="localhost")
        with pytest.raises(ClientError) as error:
            await asyncio.to_thread(client.request, "status", {})
        assert error.value.code in {"server_unreachable", "tls_handshake_failed"}
        assert error.value.transport == "tls"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_plaintext_http_cannot_use_remote_api(service, tmp_path, tls_bundle):
    """The remote listener never offers http:// administration."""
    server, port = await _start_tls_server(tmp_path, service, tls_bundle)
    try:

        def attempt() -> None:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
                client.post("/api/v1/command", json={"command": "status", "args": {}})

        with pytest.raises(httpx.RequestError):
            await asyncio.to_thread(attempt)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_remote_headers_cannot_spoof_unix_identity(service, tmp_path, tls_bundle, caplog):
    """HTTP headers are not a substitute for the verified TLS identity."""
    server, port = await _start_tls_server(tmp_path, service, tls_bundle)
    try:
        with caplog.at_level("INFO"):
            client = _remote_client(tls_bundle, port)

            def call() -> None:
                with client._http_client(timeout=5) as http:
                    http.post(
                        "/api/v1/command",
                        json={"command": "delete", "args": {}},
                        headers={"X-Chimera-Uid": "0", "X-Forwarded-User": "root"},
                    )

            await asyncio.to_thread(call)
        peer = service.execute.await_args.args[2]
        assert isinstance(peer, PeerCredentials)
        assert peer.transport == "tls"
        assert peer.uid is None
        assert "BEGIN CERTIFICATE" not in caplog.text
        assert "PRIVATE KEY" not in caplog.text
        assert f"cert_sha256={peer.cert_sha256}" in caplog.text
        assert "subject=" in caplog.text
    finally:
        await server.stop()
