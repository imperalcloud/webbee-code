"""ONE source of truth for the live terminal size and every proportional
derivation (W2 front-2: proportions, not pixels). prompt_toolkit's
output.get_size() is the ONLY reliable live read — shutil.get_terminal_size
consults the COLUMNS/LINES env vars first, so a launcher exporting them
freezes every 'live' read (recon-verified hazard). The shutil fallback exists
for headless/no-app contexts only."""
from __future__ import annotations


def get_size(app=None) -> tuple[int, int]:
    out = getattr(app, "output", None) or app
    if out is not None and hasattr(out, "get_size"):
        try:
            s = out.get_size()
            return int(s.columns), int(s.rows)
        except Exception:
            pass
    import shutil
    s = shutil.get_terminal_size((100, 24))
    return int(s.columns), int(getattr(s, "lines", 24))


def input_height_cap(rows: int) -> int:
    """The input box may grow to ≤30% of the screen (was: hardcoded 10)."""
    return max(1, min(10, rows * 3 // 10))


def panel_cap(rows: int) -> int:
    """Queue/todo item-row cap as a height fraction (was: hardcoded 5/6)."""
    return max(3, rows // 6)


def trunc(width: int, fraction: float, floor: int) -> int:
    """Width-derived truncation length (was: hardcoded [:40]/[:50]/[:60])."""
    return max(floor, int(width * fraction))
