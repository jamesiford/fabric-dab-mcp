"""Test a Data API Builder MCP endpoint end to end.

Works against any DAB config - entities and fields are discovered from the
server's own describe_entities, not hard-coded.

    python scripts/test_endpoint.py --url http://localhost:5000/mcp
    python scripts/test_endpoint.py --url https://<app>.<region>.azurecontainerapps.io/mcp
    python scripts/test_endpoint.py --url https://<internal-fqdn>/mcp --audience <ENTRA_API_URI>

With --audience a bearer token is minted with DefaultAzureCredential (your az
login locally; the managed identity inside an ACA Job), and the script also
proves a request WITHOUT a token is refused.

Exit code is non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

WRITE_TOOLS = {"create_record", "update_record", "delete_record", "execute_entity"}

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""), flush=True)
    return ok


def token_for(audience: str) -> str:
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential().get_token(f"{audience.rstrip('/')}/.default").token


def payload(result) -> dict:
    text = "".join(b.text for b in result.content if getattr(b, "type", None) == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"status": "error", "error": {"message": text[:300]}}


def rows_of(p: dict) -> list:
    r = p.get("result")
    if isinstance(r, dict):
        r = r.get("value", [])
    return r if isinstance(r, list) else []


def rejected(p: dict) -> tuple[bool, str]:
    if p.get("status") == "error":
        err = p.get("error") or {}
        return True, f"{err.get('type', 'error')}: {str(err.get('message', ''))[:90]}"
    return False, f"returned {len(rows_of(p))} rows"


async def unauthenticated_is_refused(url: str) -> None:
    """A secure endpoint must refuse an MCP initialize that carries no token."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test_endpoint", "version": "1"}},
    }
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(url, json=body,
                            headers={"Accept": "application/json, text/event-stream"})
    check(r.status_code in (401, 403), "request without a token is refused",
          f"HTTP {r.status_code}")


async def run(url: str, headers: dict) -> None:
    started = time.perf_counter()
    async with streamablehttp_client(url, headers=headers, timeout=300) as (read, write, _):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            check(True, "MCP handshake",
                  f"{init.serverInfo.name} {init.serverInfo.version}, protocol {init.protocolVersion}")

            tools = {t.name for t in (await s.list_tools()).tools}
            check("describe_entities" in tools and "read_records" in tools,
                  "read tools advertised", ", ".join(sorted(tools)))
            leaked = tools & WRITE_TOOLS
            check(not leaked, "no write tools advertised",
                  f"found {', '.join(sorted(leaked))}" if leaked else "")

            catalog = payload(await s.call_tool("describe_entities", {}))
            entities = catalog.get("entities") or []
            if not check(bool(entities), "describe_entities lists entities",
                         ", ".join(e.get("name", "?") for e in entities)):
                return
            undescribed = [
                f"{e['name']}.{f['name']}" for e in entities for f in e.get("fields", [])
                if not f.get("description") or str(f.get("description")).startswith("TODO")
            ]
            check(not undescribed, "every field has a description",
                  f"{len(undescribed)} missing, e.g. {', '.join(undescribed[:3])}" if undescribed else "")

            read_counts: dict[str, int] = {}
            for e in entities:
                p = payload(await s.call_tool("read_records", {"entity": e["name"]}))
                bad, why = rejected(p)
                paged = bool((p.get("result") or {}).get("after")) if isinstance(p.get("result"), dict) else False
                read_counts[e["name"]] = len(rows_of(p))
                check(not bad and not paged, f"read {e['name']} returns a complete set",
                      why if bad else f"{len(rows_of(p)):,} rows" + (", but PAGED" if paged else ""))

            first = entities[0]
            fields = [f["name"] for f in first.get("fields", [])]
            p = payload(await s.call_tool("aggregate_records", {
                "entity": first["name"], "function": "count", "field": "*",
            }))
            bad, why = rejected(p)
            agg = rows_of(p) or ([p.get("result")] if isinstance(p.get("result"), dict) else [])
            counted = next((v for v in (agg[0] if agg else {}).values() if isinstance(v, (int, float))), None)
            check(not bad and counted == read_counts.get(first["name"]),
                  f"count(*) on {first['name']} matches rows read",
                  why if bad else f"count {counted} vs read {read_counts.get(first['name'])}")

            print("\n  guardrails - each must be rejected by DAB:")
            hostile = [
                ("entity not in config", "read_records", {"entity": "definitely_not_published"}),
                ("field not in entity", "read_records",
                 {"entity": first["name"], "select": "definitely_not_a_column"}),
                ("injection through groupby", "aggregate_records",
                 {"entity": first["name"], "function": "count", "field": "*",
                  "groupby": [f"{fields[0]}; DROP TABLE x--"]}),
                ("operator outside OData", "read_records",
                 {"entity": first["name"], "filter": f"{fields[0]} regex '.*'"}),
            ]
            for label, tool, args in hostile:
                bad, why = rejected(payload(await s.call_tool(tool, args)))
                check(bad, f"rejects {label}", why)

    print(f"\n  finished in {time.perf_counter() - started:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="the MCP endpoint, ending in /mcp")
    ap.add_argument("--audience", help="Entra ID app ID URI; mints a bearer token and tests refusal")
    ap.add_argument("--role", help="send X-MS-API-ROLE to act as a specific app role")
    args = ap.parse_args()

    headers: dict[str, str] = {}
    print(f"testing {args.url}\n")
    if args.audience:
        asyncio.run(unauthenticated_is_refused(args.url))
        headers["Authorization"] = f"Bearer {token_for(args.audience)}"
    if args.role:
        headers["X-MS-API-ROLE"] = args.role

    try:
        asyncio.run(run(args.url, headers))
    except Exception as exc:  # noqa: BLE001
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        check(False, "endpoint reachable", f"{type(exc).__name__}: {str(exc)[:300]}")

    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
