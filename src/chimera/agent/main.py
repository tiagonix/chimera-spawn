"""Main agent implementation."""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from watchfiles import awatch

from chimera.agent.config import ConfigManager
from chimera.agent.engine import StateEngine
from chimera.agent.ipc import IPCServer
from chimera.providers import ProviderRegistry
from chimera.utils.logging import setup_logging


logger = logging.getLogger(__name__)


class ChimeraAgent:
    """Main agent orchestrating the system."""
    
    def __init__(self, config_dir: Optional[Path] = None):
        """Initialize the agent."""
        self.config_dir = config_dir or Path("./configs")
        self.state_dir = Path("./state")
        self.config_manager: Optional[ConfigManager] = None
        self.state_engine: Optional[StateEngine] = None
        self.ipc_server: Optional[IPCServer] = None
        self.shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        
    async def initialize(self):
        """Initialize agent components."""
        # Ensure directories exist
        self.state_dir.mkdir(exist_ok=True)
        
        # Initialize configuration
        self.config_manager = ConfigManager(self.config_dir)
        await self.config_manager.load()
        
        # Setup logging
        config = self.config_manager.config
        setup_logging(config.agent.log_level)
        
        # Initialize provider registry
        registry = ProviderRegistry()
        await registry.initialize(config)
        
        # Initialize state engine
        self.state_engine = StateEngine(
            config_manager=self.config_manager,
            provider_registry=registry,
        )
        
        # Initialize IPC server
        socket_path = Path(config.agent.socket_path)
        if socket_path.is_absolute():
            ipc_socket = socket_path
        else:
            ipc_socket = self.state_dir / socket_path.name
            
        self.ipc_server = IPCServer(
            socket_path=ipc_socket,
            state_engine=self.state_engine,
            config_manager=self.config_manager,
        )
        
        logger.info("Agent initialized successfully")
        
    async def run(self):
        """Run the agent main loop."""
        await self.initialize()
        
        # Setup signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.shutdown)
            
        # Start components
        try:
            # Start IPC server
            server_task = asyncio.create_task(self.ipc_server.start())
            self._tasks.append(server_task)
            
            # Start reconciliation loop
            reconcile_task = asyncio.create_task(self._reconciliation_loop())
            self._tasks.append(reconcile_task)
            
            # Start config watcher
            config_task = asyncio.create_task(self._config_watch_loop())
            self._tasks.append(config_task)
            
            logger.info("Agent started, waiting for shutdown signal")
            await self.shutdown_event.wait()
            
        finally:
            await self._cleanup()
            
    async def _reconciliation_loop(self):
        """Run periodic reconciliation."""
        config = self.config_manager.config
        interval = config.agent.reconciliation_interval
        
        while not self.shutdown_event.is_set():
            try:
                logger.debug("Starting reconciliation cycle")
                await self.state_engine.reconcile()
                logger.debug("Reconciliation cycle completed")
            except Exception as e:
                logger.error(f"Reconciliation error: {e}", exc_info=True)
                
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(),
                    timeout=interval
                )
            except asyncio.TimeoutError:
                continue
                
    async def _config_watch_loop(self):
        """Watch for configuration changes."""
        logger.info(f"Starting config watcher on {self.config_manager.config_dir}")
        try:
            async for changes in awatch(self.config_manager.config_dir, stop_event=self.shutdown_event):
                logger.info("Configuration changed, reloading")
                try:
                    await self.config_manager.load()
                    # Trigger immediate reconciliation
                    asyncio.create_task(self.state_engine.reconcile())
                except Exception as e:
                    logger.error(f"Failed to reload configuration: {e}")
        except Exception as e:
            # Handle cancellation gracefully
            if not self.shutdown_event.is_set():
                logger.error(f"Config watch error: {e}", exc_info=True)
            
    def shutdown(self):
        """Signal shutdown."""
        logger.info("Shutdown requested")
        self.shutdown_event.set()
        
    async def _cleanup(self):
        """Clean up resources."""
        logger.info("Cleaning up agent resources")
        
        # Cancel all tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()
                
        # Wait for tasks to complete
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            
        # Stop IPC server
        if self.ipc_server:
            await self.ipc_server.stop()
            
        logger.info("Agent cleanup completed")


async def run_agent():
    """Run the agent."""
    # Allow config dir override from environment
    import os
    config_dir = os.environ.get("CHIMERA_CONFIG_DIR")
    if config_dir:
        config_path = Path(config_dir)
    else:
        config_path = None
        
    agent = ChimeraAgent(config_dir=config_path)
    await agent.run()
