"""
Configuration models.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

from pydantic import Field

from chimera.pydantic_compat import IgnoreExtraModel, PYDANTIC_V2, validated_field

if PYDANTIC_V2:
    from pydantic import model_validator
else:
    from pydantic import root_validator


class ServerTlsConfig(IgnoreExtraModel):
    """Mutual TLS material for the optional remote listener."""

    certificate: str
    private_key: str
    client_ca: str


class ServerConfig(IgnoreExtraModel):
    """Server configuration."""

    reconciliation_interval: int = Field(default=30, ge=5)
    log_level: str = Field(default="INFO")
    admin_group: str = Field(default="chimera-admin")
    host: str | None = None
    port: int = Field(default=8080, ge=1, le=65535)
    tls: ServerTlsConfig | None = None

    @validated_field("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Invalid log level: {v}")
        return v.upper()

    @validated_field("host")
    @classmethod
    def validate_host(cls, v: str | None) -> str | None:
        """Treat null as disabled and reject URL-shaped bind addresses."""
        if v is None:
            return None
        host = v.strip()
        if not host:
            return None
        if "://" in host or "/" in host or "?" in host:
            raise ValueError("server.host must be a bind address, not a URL")
        return host

    def require_complete_remote_tls(self) -> None:
        """A configured remote bind address requires complete TLS files."""
        if self.host is None:
            return
        if self.tls is None:
            raise ValueError(
                "server.host requires server.tls.certificate, server.tls.private_key, "
                "and server.tls.client_ca"
            )

    if PYDANTIC_V2:

        @model_validator(mode="after")
        def _require_remote_tls(self) -> "ServerConfig":
            self.require_complete_remote_tls()
            return self

    else:

        @root_validator  # type: ignore[call-overload,unused-ignore]
        def _require_remote_tls(cls, values: dict[str, object]) -> dict[str, object]:
            if values.get("host") is not None and values.get("tls") is None:
                raise ValueError(
                    "server.host requires server.tls.certificate, server.tls.private_key, "
                    "and server.tls.client_ca"
                )
            return values


class ProxyConfig(IgnoreExtraModel):
    """Proxy configuration."""

    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str = Field(default="localhost,127.0.0.1")


class SystemdConfig(IgnoreExtraModel):
    """Systemd paths configuration."""

    machines_dir: str = Field(default="/var/lib/machines")
    nspawn_dir: str = Field(default="/etc/systemd/nspawn")
    system_dir: str = Field(default="/etc/systemd/system")


class ChimeraConfig(IgnoreExtraModel):
    """Main configuration model."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    proxy: ProxyConfig | None = None
    systemd: SystemdConfig = Field(default_factory=SystemdConfig)
