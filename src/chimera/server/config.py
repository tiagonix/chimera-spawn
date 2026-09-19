"""
Configuration management for the server.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML

from chimera.errors import ChimeraError
from chimera.models.config import ChimeraConfig
from chimera.models.image import ImageSpec
from chimera.models.profile import ProfileSpec

logger = logging.getLogger(__name__)


@dataclass
class CatalogSnapshot:
    """A completely parsed catalog, ready to replace the active snapshot."""

    config: ChimeraConfig
    images: dict[str, ImageSpec]
    profiles: dict[str, ProfileSpec]
    cloud_init_templates: dict[str, dict[str, Any]]
    source_files: list[Path]


class ConfigManager:
    """Load static service configuration and layered image/profile catalogs."""

    def __init__(self, config_dir: Path, catalog_dir: Path | None = None):
        """Initialize configuration manager."""
        self.config_dir = Path(config_dir)
        self.catalog_dir = Path(catalog_dir) if catalog_dir else None
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.config: ChimeraConfig | None = None
        self.images: dict[str, ImageSpec] = {}
        self.profiles: dict[str, ProfileSpec] = {}
        self.cloud_init_templates: dict[str, dict[str, Any]] = {}
        self.source_files: list[Path] = []
        self.snapshot: CatalogSnapshot | None = None

    async def build_snapshot(self) -> CatalogSnapshot:
        """Read and validate a candidate snapshot without changing live state.

        Container definitions deliberately do not load here. CLI-managed
        lifecycle intent lives in the durable state store, not node YAML.
        """
        logger.info(f"Loading configuration from {self.config_dir}")
        return await asyncio.to_thread(self._load_snapshot)

    def apply_snapshot(self, snapshot: CatalogSnapshot) -> None:
        """Atomically install a fully validated candidate snapshot."""
        if self.snapshot is not None:
            restart_required: list[str] = []
            current = self.snapshot.config
            candidate = snapshot.config
            if candidate.systemd != current.systemd:
                restart_required.append("systemd storage paths")
            if candidate.proxy != current.proxy:
                restart_required.append("proxy configuration")
            if candidate.server.admin_group != current.server.admin_group:
                restart_required.append("server administrator group")
            if candidate.server.log_level != current.server.log_level:
                restart_required.append("server log level")
            if candidate.server.host != current.server.host:
                restart_required.append("remote listener host")
            if candidate.server.port != current.server.port:
                restart_required.append("remote listener port")
            if candidate.server.tls != current.server.tls:
                restart_required.append("remote TLS material")
            if restart_required:
                changed = ", ".join(restart_required)
                raise ChimeraError(
                    code="restart_required",
                    message=f"Configuration changes require a server restart: {changed}.",
                    suggestion="Restore the previous values or restart chimera-server after updating them.",
                    status=409,
                )

        self.snapshot = snapshot
        self.config = snapshot.config
        self.images = snapshot.images
        self.profiles = snapshot.profiles
        self.cloud_init_templates = snapshot.cloud_init_templates
        self.source_files = snapshot.source_files
        logger.info("Configuration loaded successfully")

    async def load(self) -> None:
        """Build then apply a snapshot for startup compatibility."""
        snapshot = await self.build_snapshot()
        self.apply_snapshot(snapshot)

    def _load_snapshot(self) -> CatalogSnapshot:
        """Read all source files synchronously inside an asyncio worker thread."""
        config_file = self._main_config_file()
        try:
            main_data = self._read_yaml_sync(config_file) or {}
        except OSError as error:
            raise ChimeraError(
                code="invalid_configuration",
                message="Chimera configuration is invalid.",
                detail=str(error),
                suggestion="Run 'chimeractl config validate' after correcting the named file.",
                status=422,
            ) from error
        try:
            config = ChimeraConfig(**main_data)
            images, image_files = self._load_models("images", ImageSpec)
            profiles, profile_files = self._load_models("profiles", ProfileSpec)
            cloud_init_templates, cloud_init_files = self._load_templates()
        except (OSError, ValidationError, ValueError, TypeError) as error:
            raise ChimeraError(
                code="invalid_configuration",
                message="Chimera configuration is invalid.",
                detail=str(error),
                suggestion="Run 'chimeractl config validate' after correcting the named file.",
                status=422,
            ) from error

        return CatalogSnapshot(
            config=config,
            images=images,
            profiles=profiles,
            cloud_init_templates=cloud_init_templates,
            source_files=[config_file, *image_files, *profile_files, *cloud_init_files],
        )

    async def _read_yaml(self, file_path: Path) -> dict[str, Any]:
        """Read and parse YAML file asynchronously."""
        return await asyncio.to_thread(self._read_yaml_sync, file_path)

    def _main_config_file(self) -> Path:
        """Return the required main configuration file."""
        return self.config_dir / "chimera.yaml"

    def _catalog_directories(self, resource_type: str) -> list[Path]:
        """Return packaged defaults first and local overrides second."""
        locations: list[Path] = []
        if self.catalog_dir and self.catalog_dir != self.config_dir:
            locations.append(self.catalog_dir / resource_type)
        locations.append(self.config_dir / resource_type)
        return locations

    def _load_models(
        self, resource_type: str, model_type: Any
    ) -> tuple[dict[str, Any], list[Path]]:
        """Load model declarations, letting local files override package defaults."""
        resources: dict[str, Any] = {}
        source_files: list[Path] = []
        for directory in self._catalog_directories(resource_type):
            if not directory.exists():
                continue
            for yaml_file in sorted(directory.glob("*.yaml")):
                data = self._read_yaml_sync(yaml_file) or {}
                if not isinstance(data, dict):
                    raise ValueError(f"{yaml_file} must contain a mapping")
                for name, spec in data.items():
                    if not isinstance(name, str) or not isinstance(spec, dict):
                        raise ValueError(f"{yaml_file} entries must map names to mappings")
                    resources[name] = model_type(name=name, **spec)
                source_files.append(yaml_file)
        return resources, source_files

    def _load_templates(self) -> tuple[dict[str, dict[str, Any]], list[Path]]:
        """Load cloud-init templates, letting local files override defaults."""
        templates: dict[str, dict[str, Any]] = {}
        source_files: list[Path] = []
        for directory in self._catalog_directories("cloud-init"):
            if not directory.exists():
                continue
            for yaml_file in sorted(directory.glob("*.yaml")):
                data = self._read_yaml_sync(yaml_file) or {}
                if not isinstance(data, dict):
                    raise ValueError(f"{yaml_file} must contain a mapping")
                for name, template in data.items():
                    if not isinstance(name, str) or not isinstance(template, dict):
                        raise ValueError(f"{yaml_file} templates must map names to mappings")
                    templates[name] = dict(template)
                source_files.append(yaml_file)
        return templates, source_files

    def _read_yaml_sync(self, file_path: Path) -> dict[str, Any]:
        """Read and parse one YAML source file."""
        data = self.yaml.load(file_path.read_text(encoding="utf-8"))
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"{file_path} must contain a mapping")
        return dict(data)

    def get_image_spec(self, name: str) -> ImageSpec | None:
        """Get image specification by name."""
        return self.images.get(name)

    def get_profile_spec(self, name: str) -> ProfileSpec | None:
        """Get profile specification by name."""
        return self.profiles.get(name)
