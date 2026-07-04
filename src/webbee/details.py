"""Step drill-down client — P1b. Fetches the PII-masked step-detail record
(gateway /v1/activity/steps/{ref}) for the terminal surface. House pattern
= sessions.py: (cfg, token_provider), lazy httpx, best-effort (errors ->
{} so the REPL notes the failure instead of crashing)."""
from __future__ import annotations


def build_step_ref(session_id: str, step_id: str) -> str:
    """terminal:{session_id}:{step_id} — mirrors the kernel ref grammar."""
    return f"terminal:{session_id}:{step_id}"


def format_steps(steps: list[dict]) -> str:
    """Numbered step list for /steps (facts only; label is app·tool)."""
    if not steps:
        return "No steps recorded in the last turn."
    lines = ["Last turn steps (`/steps N` or Up/Down + Enter to expand):"]
    for i, s in enumerate(steps, 1):
        mark = "✓" if s.get("ok") else "✗"
        lines.append(f"  {i}. {mark} {s.get('label', '')}")
    return "\n".join(lines)


async def fetch_step_detail(cfg, token_provider, ref: str) -> dict:
    try:
        import httpx

        token = await token_provider()
        async with httpx.AsyncClient(base_url=cfg.api_url, timeout=10) as c:
            r = await c.get(f"/v1/activity/steps/{ref}",
                            headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}
