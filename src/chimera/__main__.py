"""Main entry point dispatcher for chimera commands."""

import sys


def main():
    """Dispatch to appropriate submodule based on command."""
    print("Use 'python -m chimera.agent' to run the agent")
    print("Use 'python -m chimera.cli' for the command-line interface")
    sys.exit(1)


if __name__ == "__main__":
    main()
