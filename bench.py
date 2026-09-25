"""Head-to-head latency benchmark: Fabric data agent vs Data API Builder.

Both paths answer the same questions against the same Lakehouse, and both are
reached over MCP. What differs is how the question becomes a query:

  data agent : NL -> hosted planner -> generated SQL/DAX -> execute -> summarise
  DAB        : NL -> tool + arguments -> DAB builds the SQL -> execute

We report the data agent end to end, because that is what the user actually waits
for, and the DAB tool call on its own. The DAB number excludes the model turn
that picks the tool and phrases the answer - see README for how to read this
honestly.

DAB must be running. Start the demo app, or:

    python src/dab_process.py

The data agent is reached over its native MCP endpoint. The older
OpenAI-assistants surface failed 45/45 and is not used.

Note on RLS: the claim-driven security policy must be OFF for this benchmark.
The data agent cannot set SESSION_CONTEXT, so a fail-closed predicate filters it
to zero rows and the comparison measures nothing. Run `setup_rls.py --disable`
first; this script asserts on it rather than producing a silently empty result.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from dab_lane import DAB_URL, _rows_from  # noqa: E402
from data_agent import DataAgentLane  # noqa: E402
from fabric_sql import FabricSqlClient  # noqa: E402

REPEATS = int(os.environ.get("BENCH_REPEATS", "1"))


def assert_rls_off() -> None:
    """Fail loudly rather than benchmark a data agent that can see nothing."""
    rows = FabricSqlClient.from_env().query(
        "SELECT COUNT(*) FROM dbo.vw_deposits"
    ).rows
    if rows and int(rows[0][0]):
        return
    sys.exit(
        "\nABORT: the deposits view returned no rows to this connection.\n"
        "The claim-driven RLS policy is probably still enabled, which filters the\n"
        "data agent to zero rows and makes the comparison meaningless.\n\n"
        "    python setup_rls.py --disable\n\n"
        "Re-enable it afterwards with:\n\n"
        "    python setup_rls.py --enable --claim\n"
    )


async def _call_dab(tool: str, args: dict) -> tuple[int, float, float]:
    """Returns (row_count, connect_s, call_s).

    The MCP handshake is timed apart from the tool call. A stateless client pays
    a fresh connect per question; folding it into the query number would
    overstate what the database costs.
    """
    t_conn = time.perf_counter()
    async with streamablehttp_client(DAB_URL, timeout=300) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            connect_s = time.perf_counter() - t_conn
            t0 = time.perf_counter()
            result = await session.call_tool(tool, args)
            call_s = time.perf_counter() - t0
    text = "".join(b.text for b in result.content if getattr(b, "type", None) == "text")
    payload = json.loads(text)
    if payload.get("status") == "error":
        raise RuntimeError((payload.get("error") or {}).get("message", "tool error"))
    return len(_rows_from(payload)), connect_s, call_s


def call_dab(tool: str, args: dict) -> tuple[int, float, float]:
    return asyncio.run(_call_dab(tool, args))


# question -> the DAB tool call that answers the same thing
CASES = [
    {
        "id": "q1",
        "question": "What is the total deposit balance for each branch, counting USD accounts only?",
        "tool": "aggregate_records",
        "args": {
            "entity": "Deposits", "function": "sum", "field": "balance",
            "groupby": ["branch_name"], "filter": "currency eq 'USD'",
        },
    },
    {
        "id": "q2",
        "question": "What is the average interest rate for each deposit product?",
        "tool": "aggregate_records",
        "args": {
            "entity": "Deposits", "function": "avg", "field": "rate",
            "groupby": ["product_name"],
        },
    },
    {
        "id": "q3",
        "question": "Which branches have term deposits maturing in the next 90 days, and how much?",
        "tool": "aggregate_records",
        "args": {
            "entity": "MaturityLadder", "function": "sum", "field": "balance",
            "groupby": ["branch_name"], "filter": "days_to_maturity le 90",
        },
    },
    {
        "id": "q4",
        "question": "Show me the five largest certificate of deposit accounts by balance.",
        "tool": "read_records",
        "args": {
            "entity": "Deposits",
            "select": "customer_name,currency,balance,matures_on",
            "filter": "product_code eq 'CD'",
            "orderby": ["balance desc"],
            "first": 5,
        },
    },
    {
        "id": "q5",
        "question": "How many deposit accounts does each branch have?",
        "tool": "aggregate_records",
        "args": {
            "entity": "Deposits", "function": "count", "field": "*",
            "groupby": ["branch_name"],
        },
    },
]


def ask_data_agent(lane: DataAgentLane, question: str) -> tuple[float, str, str]:
    """Returns (elapsed_s, status, answer_text)."""
    result = lane.ask(question)
    return (
        result.get("elapsed_s", 0.0),
        result.get("status", "unknown"),
        result.get("answer") or result.get("error", ""),
    )


def main() -> None:
    lane = DataAgentLane()
    results = []

    print("checking RLS state ...")
    assert_rls_off()

    print("warming both paths (excluded from results)...")
    ask_data_agent(lane, "How many deposit accounts are there?")
    call_dab("aggregate_records", {
        "entity": "Deposits", "function": "count", "field": "*",
        "groupby": ["product_code"],
    })

    for case in CASES:
        print(f"\n--- {case['id']}: {case['question']}")

        agent_times, agent_status, agent_answer = [], "", ""
        for _ in range(REPEATS):
            secs, status, answer = ask_data_agent(lane, case["question"])
            agent_times.append(secs)
            agent_status, agent_answer = status, answer
            print(f"    data agent : {secs:7.2f} s   [{status}]")

        mcp_times, mcp_connects, mcp_rows = [], [], 0
        for _ in range(REPEATS):
            mcp_rows, connect_s, call_s = call_dab(case["tool"], case["args"])
            mcp_times.append(call_s)
            mcp_connects.append(connect_s)
            print(f"    dab tool   : {call_s:7.2f} s   [{mcp_rows} rows, "
                  f"+{connect_s:.2f}s MCP handshake]")

        results.append({
            "id": case["id"],
            "question": case["question"],
            "agent_s": statistics.median(agent_times),
            "agent_status": agent_status,
            "agent_answer": agent_answer[:400],
            "mcp_s": statistics.median(mcp_times),
            "mcp_connect_s": statistics.median(mcp_connects),
            "mcp_rows": mcp_rows,
        })

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "bench.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)

    print("\n" + "=" * 78)
    ok = [r for r in results if r["agent_status"] == "completed"]
    if ok:
        print(f"{'':4s} {'data agent':>12s} {'mcp tool':>12s} {'speed-up':>10s}   question")
        print("=" * 78)
        for r in results:
            if r["agent_status"] != "completed":
                print(f"{r['id']:4s} {'--':>11s}  {r['mcp_s']:11.2f}s {'n/a':>10s}   "
                      f"{r['question'][:34]} ({r['agent_status']})")
                continue
            speed = f"{r['agent_s'] / r['mcp_s']:.0f}x" if r["mcp_s"] > 0 else "-"
            print(f"{r['id']:4s} {r['agent_s']:11.2f}s {r['mcp_s']:11.2f}s {speed:>10s}   "
                  f"{r['question'][:34]}")
        ma = statistics.median([r["agent_s"] for r in ok])
        mm = statistics.median([r["mcp_s"] for r in ok])
        print("-" * 78)
        print(f"{'med':4s} {ma:11.2f}s {mm:11.2f}s {ma / mm:9.0f}x")
    else:
        print("MCP path - measured. Data agent produced no successful run.")
        print("=" * 78)
        for r in results:
            print(f"{r['id']:4s} {r['mcp_s']:8.2f}s  {r['mcp_rows']:>3d} rows   {r['question'][:46]}")
        mm = statistics.median([r["mcp_s"] for r in results])
        print("-" * 78)
        print(f"median MCP tool call: {mm*1000:.0f} ms")
        print(f"\ndata agent status   : {results[0]['agent_status']}")
        print(f"data agent detail   : {results[0]['agent_answer'][:160]}")
        print("\nNo data agent timings are reported - see report.py for the published")
        print("baselines used on the chart, and README for provenance.")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
