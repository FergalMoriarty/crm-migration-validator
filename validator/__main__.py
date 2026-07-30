"""Allows the package to be run directly: `python -m validator`."""

import sys

from .cli import main

sys.exit(main())
