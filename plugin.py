"""WS_COLLAB workbench plugin entrypoint.

The workbench plugin loader imports this file directly (not as part of a package)
and calls ``create_router(manifest)``. We add this directory to ``sys.path`` so
the ``ws_collab`` package is importable, then delegate to one of two runners:

* :mod:`ws_collab.standalone` — run WS_COLLAB as its own process; the workbench
  reaches it through its ``web_proxy`` (see ``plugin.json``). This is the default.
* :mod:`ws_collab.embedded` — mount WS_COLLAB in-process and return its router.

Select the runner with ``WS_COLLAB_PLUGIN_MODE=standalone|embedded``
(default: ``standalone``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _mode() -> str:
    return (os.environ.get("WS_COLLAB_PLUGIN_MODE") or "standalone").strip().lower()


def create_router(manifest: dict[str, Any] | None = None):
    """Delegate to the selected runner's ``create_router``."""

    if _mode() == "embedded":
        from ws_collab.embedded import create_router as _create_router
    else:
        from ws_collab.standalone import create_router as _create_router
    return _create_router(manifest)


__all__ = ["create_router"]
