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


def panel_cap(rows: int, floor: int = 5) -> int:
    """Queue/todo item-row cap as a height fraction (was: hardcoded 5/6).
    `floor` is the caller's OWN today's-look constant (queue=5, todo=6) —
    the old fixed floor=3 shrank both panels below their pre-W2 row counts
    on an ordinary 24-row terminal, contradicting the "today's look
    preserved" claim; the per-caller floor honors it while still growing on
    tall screens."""
    return max(floor, rows // 6)


def trunc(width: int, fraction: float, floor: int) -> int:
    """Width-derived truncation length (was: hardcoded [:40]/[:50]/[:60])."""
    return max(floor, int(width * fraction))
