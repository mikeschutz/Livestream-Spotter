"""Pure edge helpers shared by flag detectors."""

from __future__ import annotations


def rising_edge(previous: int, current: int, mask: int) -> bool:
    """Return true only when the masked signal changes inactive -> active."""
    return not bool(previous & mask) and bool(current & mask)
