#!/usr/bin/env python3
"""Entry point: ``python auditor.py --test-connection``.

Thin shim so the tool has the obvious filename at the repo root while the real
code lives in the importable ``netauditor`` package (which is what the tests
import).
"""

from netauditor.cli import run_cli

if __name__ == "__main__":
    run_cli()
