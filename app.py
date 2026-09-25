"""MCP over a Fabric Lakehouse SQL endpoint - live demo app.

Run:
    python app.py          then open http://127.0.0.1:8000

Two lanes, same question, same Lakehouse.

    MCP lane        Microsoft Data API Builder publishes curated views as MCP
                    tools. The model picks a tool and fills in arguments; DAB
                    turns those arguments into SQL and runs it.
    data agent lane the question goes to the Fabric data agent over its native
                    MCP endpoint and Fabric does the planning, generation and
                    execution internally

Both sides are stock products. Neither lane contains a hand-written query
engine, which is the point: the difference is where the query comes from, not
how much bespoke code sits behind it.

Every stage is streamed to the browser as it happens so the trace panel shows
real work rather than a replay.

The data agent lane uses the agent's native MCP endpoint; its OpenAI-assistants
surface failed 45/45. See src/data_agent.py for the history.

RLS note: the claim-driven security policy filters the data agent to zero rows,
because a data agent cannot set SESSION_CONTEXT. /api/rls reports the live state
so the UI can warn instead of silently showing an empty lane.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Iterator

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))
load_dotenv(os.path.join(HERE, ".env"))

from dab_lane import DabLane  # noqa: E402
from dab_process import DabProcess  # noqa: E402
from data_agent import DataAgentLane  # noqa: E402

_dab = DabProcess(os.path.join(HERE, "dab", "dab-config.json"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    _dab.stop()


app = FastAPI(title="Fabric Lakehouse over MCP", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

_mcp_lane: DabLane | None = None
_agent_lane: DataAgentLane | None = None


def mcp_lane() -> DabLane:
    global _mcp_lane
    if _mcp_lane is None:
        _mcp_lane = DabLane()
    return _mcp_lane


def agent_lane() -> DataAgentLane:
    global _agent_lane
    if _agent_lane is None:
        _agent_lane = DataAgentLane()
    return _agent_lane


class Question(BaseModel):
    question: str


class GuardrailCall(BaseModel):
    tool: str
    args: dict


# Each preset is sized to return a COMPLETE result set - no truncation, no
# top-N. Cardinalities were measured against the live view, not assumed:
#   branch 9 | product 5 | currency 5 | country 3 | RM 10 | industry 10
#   branch x product 45 | branch x currency 43
#   full book 8,123 rows | CDs 1,560 rows
# segment is deliberately absent: every one of the 8,123 accounts is
# 'commercial', so grouping by it returns a single uninteresting row.
#
# The first two are raw-row reads of the whole table. They are the proof that
# nothing is capped: the table beside the answer renders all 8,123 rows. The
# phrasing model still only sees a sample of them and says so.
PRESETS = [
    {"label": "Every deposit account", "rows": 8123,
     "question": "Show the complete list of deposit accounts - every row in the book, nothing left out."},
    {"label": "Every CD account", "rows": 1560,
     "question": "Show the complete list of accounts whose product code is CD. Every row, not a sample."},
    {"label": "Balance by branch", "rows": 9,
     "question": "What is the total deposit balance for every branch name? List all of them."},
    {"label": "Branch x product grid", "rows": 45,
     "question": "Show the total deposit balance for every combination of branch name and product code. Include all combinations."},
    {"label": "USD only, by branch", "rows": 7,
     "question": "What is the total deposit balance for each branch name, counting USD accounts only? Show every branch."},
    {"label": "Balance by currency", "rows": 5,
     "question": "What is the total deposit balance in each currency? Show every currency."},
    {"label": "Average rate by product", "rows": 5,
     "question": "What is the average interest rate for each product name? Show all products."},
    {"label": "Book by relationship manager", "rows": 10,
     "question": "What is the total deposit balance for each relationship manager? List every one."},
    {"label": "Balance by industry", "rows": 10,
     "question": "What is the total deposit balance by customer industry? Show all industries."},
    {"label": "Branch x currency", "rows": 43,
     "question": "Show the total deposit balance for every branch name and currency combination."},
    {"label": "Maturity ladder, 12 months", "rows": 9,
     "question": "Which branches have term deposits maturing in the next 365 days, and how much? Show every branch."},
    {"label": "Accounts by branch", "rows": 9,
     "question": "How many deposit accounts does each branch name have? List all branches."},
]

# Hostile tool calls, sent straight to DAB with the model bypassed. The point is
# that DAB rejects them, not that a well-behaved model declines to ask. Each
# one is a different class of attack on the published surface.
GUARDRAILS = [
    {
        "label": "Unpublished column",
        "blurb": "Ask for a column that was never published.",
        "tool": "read_records",
        "args": {"entity": "Deposits", "select": "tax_id"},
    },
    {
        "label": "Unpublished base table",
        "blurb": "Reach past the curated views to the customer table.",
        "tool": "read_records",
        "args": {"entity": "customer"},
    },
    {
        "label": "SQL injection via identifier",
        "blurb": "Smuggle a DROP through a group-by name.",
        "tool": "aggregate_records",
        "args": {
            "entity": "Deposits",
            "function": "sum",
            "field": "balance",
            "groupby": ["branch_name; DROP TABLE customer--"],
        },
    },
    {
        "label": "Unsupported operator",
        "blurb": "Use an operator outside the published filter grammar.",
        "tool": "read_records",
        "args": {"entity": "Deposits", "filter": "branch_name regex '.*'"},
    },
]


def _sse(events: Iterator[dict]) -> StreamingResponse:
    def body() -> Iterator[str]:
        for event in events:
            yield f"data: {json.dumps(event, default=str)}\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/presets")
def presets() -> dict:
    return {"presets": PRESETS, "guardrails": GUARDRAILS}


@app.post("/api/stream")
def stream(q: Question) -> StreamingResponse:
    return _sse(mcp_lane().ask_stream(q.question))


@app.post("/api/stream/agent")
def stream_agent(q: Question) -> StreamingResponse:
    """The Fabric data agent lane, over its native MCP endpoint."""
    return _sse(agent_lane().ask_stream(q.question))


@app.get("/api/rls")
def rls_state() -> JSONResponse:
    """Report whether the fail-closed policy is filtering the data agent to zero.

    A data agent has no way to set SESSION_CONTEXT, so when the claim-driven
    predicate is live it sees nothing. Surfacing this stops the UI from showing
    an empty lane that looks like a data agent failure when it is actually our
    own security policy doing its job.
    """
    from fabric_sql import FabricSqlClient

    try:
        rows = FabricSqlClient.from_env().query(
            "SELECT COUNT(*) FROM dbo.vw_deposits"
        ).rows
        visible = int(rows[0][0]) if rows else 0
        return JSONResponse({
            "rls_blocking_agent": visible == 0,
            "visible_rows": visible,
            "hint": (
                "Claim-driven RLS is live; the data agent lane will return no data. "
                "Run: python setup_rls.py --disable"
            ) if visible == 0 else None,
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


class SqlLookup(BaseModel):
    entity: str
    since_utc: str


@app.post("/api/sql")
def actual_sql(lookup: SqlLookup) -> JSONResponse:
    """The statement DAB actually ran, read back from Fabric's query history.

    DAB does not return its generated SQL over MCP, and Fabric publishes it to
    query history on a lag. The UI asks for it after the answer has landed so
    the lane is never held open waiting.
    """
    return JSONResponse(mcp_lane().actual_sql(lookup.entity, lookup.since_utc))


@app.post("/api/guardrail")
def guardrail(call: GuardrailCall) -> StreamingResponse:
    return _sse(mcp_lane().guardrail_stream(call.tool, call.args))


@app.post("/api/mcp")
def run_mcp(q: Question) -> JSONResponse:
    """Non-streaming fallback, kept for bench/report scripts."""
    return JSONResponse(mcp_lane().ask(q.question))


@app.post("/api/agent")
def run_agent(q: Question) -> JSONResponse:
    """Non-streaming data agent call, kept for bench/report scripts."""
    return JSONResponse(agent_lane().ask(q.question))


@app.post("/api/warm")
def warm() -> dict:
    mcp_lane().warm()
    return {"warmed": True}


if __name__ == "__main__":
    import uvicorn

    # Port is configurable because 8000 is contested on this machine - other
    # projects bind it. Without this, `python app.py` fails to bind and the
    # browser silently lands on whatever else is already listening.
    port = int(os.environ.get("PORT", "8000"))

    print("starting Data API Builder ...")
    _dab.start()
    print(f"  DAB {'adopted' if _dab.adopted else 'started'} on {os.environ.get('DAB_MCP_URL', 'http://localhost:5000/mcp')}")
    print("warming the MCP lane ...")
    mcp_lane().warm()
    print("warming the data agent lane ...")
    agent_lane().warm()
    print(f"ready -> http://127.0.0.1:{port}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        _dab.stop()
