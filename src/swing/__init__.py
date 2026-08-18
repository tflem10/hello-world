"""swing — an evidence-gated swing-trading pick system.

Nothing here imports submodules eagerly: the CLI and every entry point use lazy
imports so that ``swing --help`` stays fast and works even when optional pieces
of the system are not installed yet.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
