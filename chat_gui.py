#!/usr/bin/env python3
"""Backward-compatible web-GUI entry point.

The implementation now lives in the ``multi_llm`` package. This thin wrapper
is kept so the documented ``python chat_gui.py`` command keeps working.
"""

import sys

from multi_llm.gui import main

if __name__ == "__main__":
    sys.exit(main())
