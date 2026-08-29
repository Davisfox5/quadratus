#!/usr/bin/env python3
"""Backward-compatible CLI entry point.

The implementation now lives in the ``quadratus`` package. This thin wrapper
is kept so the documented ``python multi_model_workflow.py "..."`` command
keeps working.
"""

import sys

from quadratus.cli import main

if __name__ == "__main__":
    sys.exit(main())
