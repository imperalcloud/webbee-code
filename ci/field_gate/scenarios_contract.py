"""One tool contract across shells (ICNLI §3.5): the same capability has one name set and one parameter set.

Sources compared, all as shipped: the kernel's schema for Webbee (snapshot of LOCAL_TOOL_DEFS at kernel 20a3c72b until
the contract moves into the SDK), webbee's executor methods, imperal-mcp's tool list.
"""
from __future__ import annotations


from ci.field_gate.registry import Ctx, Outcome, scenario



@scenario("contract-one-schema", ("R3-ckpt-root", "R3-П14", "R3-Н4"), shells=("mcp",))
async def contract_one_schema(ctx: Ctx) -> Outcome:
    """The SDK contract is the one description of every shell tool (ICNLI 3.5): imperal-mcp lists exactly its mcp tools
    with exactly their parameters, and Webbee's executor answers every webbee tool the client runs (the kernel answers
    the rest and shows Webbee's model the same contract)."""
    from imperal_sdk import tool_contract as tc
    from webbee.tools import LocalToolExecutor
    executor = {n[3:] for n in dir(LocalToolExecutor) if n.startswith("_t_")}
    listed = {t.name: sorted((t.inputSchema or {}).get("properties", {}))
              for t in (await ctx.mcp._session.list_tools()).tools}
    problems = []
    mcp_contract = {t.name: sorted(t.params) for t in tc.shell_tools("mcp")}
    for name in sorted(set(mcp_contract) ^ set(listed)):
        problems.append(f"{name}: {'in the contract, not listed by imperal-mcp' if name in mcp_contract else 'listed by imperal-mcp, not in the contract'}")
    for name in sorted(set(mcp_contract) & set(listed)):
        if mcp_contract[name] != listed[name]:
            problems.append(f"{name}: contract {mcp_contract[name]} vs imperal-mcp {listed[name]}")
    for t in tc.shell_tools("webbee"):
        if t.executed_by == "client" and t.name not in executor:
            problems.append(f"{t.name}: Webbee's model is given it, Webbee's executor has no method for it")
    return Outcome(not problems, "one name and one parameter set per tool in every shell",
                   "; ".join(problems)[:1500] or "all agree")
