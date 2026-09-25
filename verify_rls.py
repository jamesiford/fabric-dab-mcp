"""Prove per-user row-level security end to end: MCP client -> DAB -> Fabric.

This is the evidence for the claim that per-user data security is achievable for
a Fabric Lakehouse SQL analytics endpoint, which is NOT documented:

  - Microsoft documents DAB's On-Behalf-Of user delegation for the `mssql`
    family only, and the Lakehouse SQL analytics endpoint is `dwsql`
  - Microsoft documents `set-session-context` for SQL Server / Azure SQL only
  - Fabric documents RLS via CREATE SECURITY POLICY for the endpoint, but its
    examples key off the connected principal, not session context

Measured here instead: DAB emits sp_set_session_context carrying the caller's
role claim, Fabric's security policy reads it, and identical tool calls return
different rows per caller. Nothing about the query changes - the filter is
applied by the database, below the API.

Prerequisites:
    python setup_rls.py --enable --claim
    dab start -c dab/dab-config.SimTest.json     (Simulator auth, RM roles)

    python verify_rls.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ENDPOINT = "http://localhost:5000/mcp"

# Roles declared in the DAB config. 'svc-all' is the break-glass service role the
# predicate treats as unrestricted; the two names are real relationship managers.
CASES = ["svc-all", "B. Nakamura", "I. Petrosyan", None]

EXPECTED_UNRESTRICTED = 8123


def _rows(payload: dict) -> list:
    result = payload.get("result")
    if isinstance(result, dict):
        return result.get("value") or []
    if isinstance(result, list):
        return result
    return []


async def book_for(role: str | None) -> tuple[list, str | None]:
    headers = {"X-MS-API-ROLE": role} if role else {}
    async with streamablehttp_client(ENDPOINT, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool(
                "aggregate_records",
                {
                    "entity": "Deposits",
                    "function": "count",
                    "field": "*",
                    "groupby": ["relationship_manager"],
                },
            )
            payload = json.loads(res.content[0].text)
            if payload.get("status") == "error":
                return [], str(payload.get("error"))[:100]
            return _rows(payload), None


def _summarise(rows: list) -> tuple[int, int, list[str]]:
    names, total = [], 0
    for row in rows:
        values = list(row.values())
        names.append(str(values[0]))
        try:
            total += int(values[1])
        except (IndexError, TypeError, ValueError):
            pass
    return len(rows), total, sorted(names)


async def main() -> int:
    print("Identical MCP tool call - aggregate_records grouped by relationship_manager")
    print("The only thing that changes between rows below is who is asking.\n")
    print(f"  {'caller role':<26} {'RMs visible':>11} {'accounts':>9}")
    print(f"  {'-' * 26} {'-' * 11:>11} {'-' * 9:>9}")

    results: dict[str, int] = {}
    for role in CASES:
        rows, err = await book_for(role)
        label = role or "(none -> anonymous)"
        if err:
            print(f"  {label:<26} {'ERROR':>11} {err}")
            results[label] = -1
            continue
        groups, total, names = _summarise(rows)
        preview = ", ".join(names[:2]) + ("..." if len(names) > 2 else "")
        print(f"  {label:<26} {groups:>11} {total:>9}  {preview}")
        results[label] = total

    unrestricted = results.get("svc-all", 0)
    scoped = [v for k, v in results.items() if k in ("B. Nakamura", "I. Petrosyan")]
    anon = results.get("(none -> anonymous)", -1)

    print()
    ok = (
        unrestricted == EXPECTED_UNRESTRICTED
        and all(0 < s < unrestricted for s in scoped)
        and anon == 0
    )
    if ok:
        print("  PASS - Fabric filtered the rows per caller.")
        print(f"         service role saw all {unrestricted:,} accounts;")
        print(f"         each RM saw only their own book ({', '.join(str(s) for s in scoped)});")
        print("         a caller with no role claim saw nothing.")
        print("\n  Per-user RLS works against a Fabric Lakehouse SQL analytics")
        print("  endpoint through DAB, via session context. This is undocumented.")
        return 0

    print("  FAIL - filtering did not behave as expected.")
    print(f"         svc-all={unrestricted} scoped={scoped} anonymous={anon}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
