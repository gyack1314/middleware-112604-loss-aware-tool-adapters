#!/usr/bin/env python3
"""Convenience launcher: ``python3 proxy_cli.py --help``."""

import sys
from pathlib import Path

# Allow ``python3 -I proxy_cli.py`` even though isolated mode omits the script
# directory from sys.path.  Only this trusted project-root directory is added.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tool_proxy.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
