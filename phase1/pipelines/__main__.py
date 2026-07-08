# SPDX-License-Identifier: MIT
# © 2024-2026 Autonomous Drug Repurposing Platform — Team Cosmic / VentureLab
"""CLI entry point for ``python -m pipelines``.

This file enables the ``python -m pipelines <command>`` CLI. It delegates
to :func:`pipelines._main` defined in :mod:`pipelines.__init__`.

Commands::

    list             — list all available pipelines
    run <name>       — run a single pipeline by source_name
    validate         — run validate_infrastructure()
    security         — run _validate_security()
    health           — run health_check()
    version          — print __version__
"""

from __future__ import annotations

import sys

from pipelines import _main

if __name__ == "__main__":
    _main(sys.argv[1:])
