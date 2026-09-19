"""
Server entry point for running chimera-server.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import argparse
import asyncio
import sys

from chimera.server.main import run_server


def main() -> None:
    """Run the Chimera server."""
    parser = argparse.ArgumentParser(
        prog="chimera-server",
        description="Run the local Chimera Spawn container management server.",
    )
    parser.add_argument(
        "--config-dir",
        help="Configuration directory.",
    )
    parser.add_argument(
        "--state-dir",
        help="Durable state directory.",
    )
    parser.add_argument(
        "--socket",
        help="Unix socket path.",
    )
    parser.add_argument(
        "--catalog-dir",
        help="Packaged/default catalog directory.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(
            run_server(
                config_dir=args.config_dir,
                state_dir=args.state_dir,
                socket_path=args.socket,
                catalog_dir=args.catalog_dir,
            )
        )
    except KeyboardInterrupt:
        print("\nServer shutdown requested")
        sys.exit(0)
    except Exception as e:
        print(f"Server error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
