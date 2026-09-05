"""Small presentation helpers shared by the CLI and the wizard.

Both surfaces report sizes, and they used to do it with their own copy of the
same function.  The copies disagreed -- 1536 bytes printed as "1.5 KB" in the
dedupe report and "2 KB" in the wizard summary, one screen apart in a single
session -- which reads as a bug in the numbers rather than in the formatting.
One helper, so a byte count means the same thing everywhere.
"""

from __future__ import annotations

import shutil
from pathlib import Path

__all__ = ["format_bytes", "free_bytes"]


def format_bytes(count: int) -> str:
    """Bytes at a readable scale.

    A dedupe on a small library reclaims megabytes, and reporting that as
    "0.00 GB" reads as "this did nothing".
    """
    size = float(count)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def free_bytes(path: Path) -> int:
    """Free space on the filesystem holding ``path``."""
    return shutil.disk_usage(path).free
