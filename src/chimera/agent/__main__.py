"""Agent entry point for running chimera agent."""

import asyncio
import sys
from chimera.agent.main import run_agent


def main():
    """Run the chimera agent."""
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        print("\nAgent shutdown requested")
        sys.exit(0)
    except Exception as e:
        print(f"Agent error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
