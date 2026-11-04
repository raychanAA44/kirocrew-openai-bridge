"""hub_boot.py — injection hook installed into KiroCrew's bundled site-packages.

Installed next to a one-line ``hub_boot.pth``, so ``site`` imports it for every
process that runs on KiroCrew's interpreter — including the gateway the desktop
shell spawns, which is started with a curated environment that carries no
``PYTHONPATH`` (verified: environment-variable injection does not reach it).

The real work lives outside the application directory, in the bridge's
``bootstrap/`` folder, so it can be edited, versioned and removed without
touching the installed application. That module is a no-op unless the process is
the ``gateway`` command, so ``mcp-core``, ``mcp-cron``, pool workers and every
other child stay exactly as they were.

Remove this file and ``hub_boot.pth`` to uninstall.
"""

from __future__ import annotations

import os
import runpy
import sys

# Replaced by install.ps1 / install.sh with the real location.
_BOOT_DIR = r"@@BOOTSTRAP_DIR@@"

try:
    if os.path.isdir(_BOOT_DIR):
        if _BOOT_DIR not in sys.path:
            sys.path.insert(0, _BOOT_DIR)
        target = os.path.join(_BOOT_DIR, "hook.py")
        if os.path.exists(target):
            runpy.run_path(target, run_name="kirocrew_openai_bridge_hook")
except Exception:  # noqa: BLE001
    # A broken hook must never keep the backend from starting.
    pass
