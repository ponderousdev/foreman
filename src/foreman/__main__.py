"""Entry point for ``python -m foreman <command> [flags]``. The installed
console script ``foreman`` (pyproject ``[project.scripts]``) is the primary
invocation; this module makes ``-m`` work identically."""

import sys

from foreman.cli import main

if __name__ == "__main__":
    sys.exit(main())
