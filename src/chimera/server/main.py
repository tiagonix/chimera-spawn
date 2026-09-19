"""
Main server implementation.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import asyncio
import grp
import logging
import signal

from watchfiles import awatch

from chimera.server.config import ConfigManager
from chimera.server.engine import StateEngine
from chimera.server.lock import StateLock
from chimera.server.api import ApiServer
from chimera.server.service import CommandService
from chimera.server.store import ContainerStore
from chimera.errors import ChimeraError
from chimera.providers import ProviderRegistry
from chimera.runtime import RuntimePaths, resolve_runtime_paths
from chimera.tls import build_server_ssl_context
from chimera.utils.logging import setup_logging

logger = logging.getLogger(__name__)


class ChimeraServer:
    """Main server orchestrating the system."""

    def __init__(self, runtime_paths: RuntimePaths | None = None):
        """Initialize the server."""
        self.runtime_paths = runtime_paths or resolve_runtime_paths()
        self.config_manager: ConfigManager | None = None
        self.state_engine: StateEngine | None = None
        self.api_server: ApiServer | None = None
        self.state_lock: StateLock | None = None
        self.shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def initialize(self) -> None:
        """Initialize server components."""
        # Create ancestor directories only. The lock opens the state directory
        # itself with O_NOFOLLOW and creates a real 0700 directory if needed.
        self.runtime_paths.state_dir.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.state_lock = StateLock(self.runtime_paths.state_dir)
        self.state_lock.acquire()

        # Initialize configuration
        self.config_manager = ConfigManager(
            self.runtime_paths.config_dir, self.runtime_paths.catalog_dir
        )
        await self.config_manager.load()

        # Setup logging
        config = self.config_manager.config
        if config is None:
            raise RuntimeError("Configuration did not produce an active snapshot")
        setup_logging(config.server.log_level)

        # Initialize provider registry
        registry = ProviderRegistry()
        await registry.initialize(config)

        # Initialize state engine
        store = ContainerStore(
            self.runtime_paths.store_path,
            state_dir_identity=self.state_lock.directory_identity,
            directory_fd=self.state_lock.directory_fd,
        )
        store.load()
        self.state_engine = StateEngine(
            config_manager=self.config_manager,
            provider_registry=registry,
            store=store,
        )

        try:
            admin_group_gid = grp.getgrnam(config.server.admin_group).gr_gid
        except KeyError:
            admin_group_gid = None

        service = CommandService(
            state_engine=self.state_engine,
            config_manager=self.config_manager,
            runtime_paths=self.runtime_paths,
            admin_group_gid=admin_group_gid,
        )
        ssl_context = None
        if config.server.host is not None:
            if config.server.tls is None:
                raise ChimeraError(
                    code="invalid_configuration",
                    message="A remote listener requires a complete TLS configuration.",
                    suggestion=(
                        "Set server.tls.certificate, server.tls.private_key, and "
                        "server.tls.client_ca, then restart chimera-server."
                    ),
                    status=500,
                )
            ssl_context = build_server_ssl_context(config.server.tls)
        self.api_server = ApiServer(
            socket_path=self.runtime_paths.socket_path,
            service=service,
            admin_group=config.server.admin_group,
            remote_host=config.server.host,
            remote_port=config.server.port,
            ssl_context=ssl_context,
        )

        logger.info("Server initialized successfully")

    async def run(self) -> None:
        """Run the server main loop."""
        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.shutdown)

        try:
            await self.initialize()
            if self.api_server is None:
                raise RuntimeError("API listener was not initialized")

            await self.api_server.start()

            reconcile_task = asyncio.create_task(self._reconciliation_loop())
            self._tasks.append(reconcile_task)

            config_task = asyncio.create_task(self._config_watch_loop())
            self._tasks.append(config_task)

            logger.info("Server started, waiting for shutdown signal")
            await self.shutdown_event.wait()
        finally:
            await self._cleanup()

    async def _reconciliation_loop(self) -> None:
        """Run periodic reconciliation."""
        if self.state_engine is None or self.config_manager is None:
            raise RuntimeError("Server components were not initialized")
        engine = self.state_engine
        config_manager = self.config_manager
        while not self.shutdown_event.is_set():
            try:
                logger.debug("Starting reconciliation cycle")
                await engine.reconcile()
                logger.debug("Reconciliation cycle completed")
            except Exception as e:
                logger.error(f"Reconciliation error: {e}", exc_info=True)

            try:
                config = config_manager.config
                if config is None:
                    raise RuntimeError("Active configuration disappeared")
                await asyncio.wait_for(
                    self.shutdown_event.wait(),
                    timeout=config.server.reconciliation_interval,
                )
            except TimeoutError:
                continue

    async def _config_watch_loop(self) -> None:
        """Watch for configuration changes."""
        if self.state_engine is None:
            raise RuntimeError("State engine was not initialized")
        engine = self.state_engine
        logger.info("Starting configuration watcher")
        try:
            async for _changes in awatch(
                self.runtime_paths.config_dir, stop_event=self.shutdown_event
            ):
                logger.info("Configuration changed, reloading")
                try:
                    await engine.reload_configuration()
                    await engine.reconcile()
                except Exception as e:
                    logger.error(f"Failed to reload configuration: {e}")
        except Exception as e:
            if not self.shutdown_event.is_set():
                logger.error(f"Config watch error: {e}", exc_info=True)

    def shutdown(self) -> None:
        """Signal shutdown."""
        logger.info("Shutdown requested")
        self.shutdown_event.set()

    async def _cleanup(self) -> None:
        """Stop accepting work, drain owned tasks, then release state authority."""
        logger.info("Cleaning up server resources")

        try:
            if self.api_server:
                await self.api_server.stop()
        finally:
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            if self.state_lock:
                self.state_lock.release()

        logger.info("Server cleanup completed")


async def run_server(
    *,
    config_dir: str | None = None,
    state_dir: str | None = None,
    socket_path: str | None = None,
    catalog_dir: str | None = None,
) -> None:
    """Run the server with installed defaults or explicit path overrides."""
    server = ChimeraServer(
        runtime_paths=resolve_runtime_paths(
            config_dir=config_dir,
            state_dir=state_dir,
            socket_path=socket_path,
            catalog_dir=catalog_dir,
        )
    )
    await server.run()
