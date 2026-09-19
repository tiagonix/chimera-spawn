"""
Main entry point dispatcher for chimera commands.

Author: Thiago Camargo <thiagocmc@proton.me>
License: AGPL-3.0-only
"""

import sys


def main() -> None:
    """Dispatch to appropriate submodule based on command."""
    print("Use 'python -m chimera.server' to run the server")
    print("Use 'python -m chimera.cli' for the command-line interface")
    sys.exit(1)


if __name__ == "__main__":
    main()
