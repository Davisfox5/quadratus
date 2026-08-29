#!/usr/bin/env python3
"""Backward-compatible web-GUI entry point.

The implementation now lives in the ``quadratus`` package. This thin wrapper
is kept so the documented ``python chat_gui.py`` command keeps working.
"""

import sys

from quadratus.gui import main

if __name__ == "__main__":
    sys.exit(main())
