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
from chimera.models.image import (
    ImageProductPolicy,
    ImageSourceSpec,
    empty_product_policy,
)
from chimera.models.profile import ProfileSpec

logger = logging.getLogger(__name__)


def _merge_image_source(packaged: dict[str, Any], site: dict[str, Any]) -> dict[str, Any]:
    """Overlay site source fields onto a packaged source before model validation."""
    merged = dict(packaged)
    for key, value in site.items():
        if key != "products":
            merged[key] = value
            continue
        if not isinstance(value, dict):
            raise ValueError("products must be a mapping")
        products = dict(merged.get("products") or {})
        for product, policy in value.items():
            products[product] = policy
        merged["products"] = products
    return merged


@dataclass
class CatalogSnapshot:
    """A completely parsed catalog, ready to replace the active snapshot."""

    config: ChimeraConfig
    image_sources: dict[str, ImageSourceSpec]
    profiles: dict[str, ProfileSpec]
    cloud_init_templates: dict[str, dict[str, Any]]
    source_files: list[Path]


class ConfigManager:
    """Load service configuration and layered image, profile, and cloud-init catalogs."""

    def __init__(self, config_dir: Path, catalog_dir: Path | None = None):
        """Initialize configuration manager."""
        self.config_dir = Path(config_dir)
        self.catalog_dir = Path(catalog_dir) if catalog_dir else None
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.config: ChimeraConfig | None = None
        self.image_sources: dict[str, ImageSourceSpec] = {}
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
        self.image_sources = snapshot.image_sources
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
            image_sources, image_source_files = self._load_image_sources()
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
            image_sources=image_sources,
            profiles=profiles,
            cloud_init_templates=cloud_init_templates,
            source_files=[
                config_file,
                *image_source_files,
                *profile_files,
                *cloud_init_files,
            ],
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

    def _load_image_sources(self) -> tuple[dict[str, ImageSourceSpec], list[Path]]:
        """Load image sources, merging site scalars and exact product policies first."""
        layered: list[dict[str, dict[str, Any]]] = []
        source_files: list[Path] = []
        for directory in self._catalog_directories("images"):
            raw, files = self._read_named_raw(directory)
            layered.append(raw)
            source_files.extend(files)
        names: list[str] = []
        for raw in layered:
            for name in raw:
                if name not in names:
                    names.append(name)
        sources: dict[str, ImageSourceSpec] = {}
        for name in names:
            spec_data: dict[str, Any] | None = None
            for raw in layered:
                layer = raw.get(name)
                if layer is None:
                    continue
                spec_data = layer if spec_data is None else _merge_image_source(spec_data, layer)
            if spec_data is None:
                continue
            sources[name] = ImageSourceSpec(name=name, **spec_data)
        return sources, source_files

    def _read_named_raw(self, directory: Path) -> tuple[dict[str, dict[str, Any]], list[Path]]:
        """Read named YAML mappings without constructing models."""
        resources: dict[str, dict[str, Any]] = {}
        source_files: list[Path] = []
        if not directory.exists():
            return resources, source_files
        for yaml_file in sorted(directory.glob("*.yaml")):
            data = self._read_yaml_sync(yaml_file) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{yaml_file} must contain a mapping")
            for name, spec in data.items():
                if not isinstance(name, str) or not isinstance(spec, dict):
                    raise ValueError(f"{yaml_file} entries must map names to mappings")
                resources[name] = dict(spec)
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

    def get_image_source_spec(self, name: str) -> ImageSourceSpec | None:
        """Get a configured SimpleStreams image source by name."""
        return self.image_sources.get(name)

    def get_product_policy(self, source_name: str, canonical_product: str) -> ImageProductPolicy:
        """Return the exact product policy, or an empty policy when none is declared."""
        source = self.image_sources.get(source_name)
        if source is None:
            return empty_product_policy()
        return source.products.get(canonical_product, empty_product_policy())

    def list_image_sources(self) -> dict[str, dict[str, Any]]:
        """Return configured SimpleStreams sources without network access."""
        sources: dict[str, dict[str, Any]] = {}
        for name, spec in self.image_sources.items():
            sources[name] = {
                "name": name,
                "url": spec.url,
                "metadata_verify": spec.metadata_verify,
                "keyring": spec.keyring,
            }
        return sources

    def get_profile_spec(self, name: str) -> ProfileSpec | None:
        """Get profile specification by name."""
        return self.profiles.get(name)
