"""MCP lane - Microsoft Data API Builder as the MCP server.

This is the honest comparison to a Fabric data agent: the model still reads the
question and still writes the answer. The only thing it does not do is write the
query. It picks one of DAB's published tools and fills in arguments; DAB turns
those arguments into SQL against a curated view.

Nothing about the tool surface is hand-written here. The tools handed to the
model are exactly what DAB advertises over MCP, and the entity catalog in the
system prompt is exactly what DAB's own `describe_entities` returns. Swap the
entities in dab-config.json and this lane follows without a code change.

Timing is reported in three parts so the audience can see where the time goes:

    route_ms   model picks a tool and its arguments
    query_ms   DAB builds the SQL and runs it on the Lakehouse SQL endpoint
    phrase_ms  model turns rows into a sentence

DAB does not return the SQL it generated. It is recovered separately, from
Fabric's own query history - see actual_sql().
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from azure.identity import AzureCliCredential, get_bearer_token_provider
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import AzureOpenAI

_API_VERSION = "2025-04-01-preview"

DAB_URL = os.environ.get("DAB_MCP_URL", "http://localhost:5000/mcp")
DAB_CONFIG = os.environ.get(
    "DAB_CONFIG",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dab", "dab-config.json"),
)

_SENTINEL = object()

# describe_entities is how DAB tells a model what exists. This lane calls it
# once at warm-up and puts the answer in the system prompt, so the per-question
# path is a single tool call rather than two round trips.
_CATALOG_TOOL = "describe_entities"

_SYSTEM = (
    "You answer questions about a bank's commercial deposit book by calling one "
    "of the supplied tools. Never invent numbers. Always call exactly one tool. "
    "Never pass a paging argument unless the user asked for a specific number of "
    "rows - omitting it returns the complete result set. "
    "Balances are a daily snapshot and are held in several currencies; do not sum "
    "across currencies without saying so.\n\n"
    "These are the entities you may read, with their fields:\n{catalog}"
)

# How many rows the phrasing model is allowed to see, budgeted by cells rather
# than rows: 45 rows x 4 columns is small, but 8,123 rows x 20 columns is not.
# Above the budget the model summarises a sample and is told to say so.
_PHRASE_ROW_BUDGET = 200
_PHRASE_CELL_BUDGET = 600

# Above this many rows an exhaustive read-out just duplicates the table rendered
# underneath it.
_ENUMERATE_LIMIT = 12


def _phrase_payload(rows: list[dict]) -> tuple[list[dict], int]:
    total = len(rows)
    if not rows:
        return [], 0
    width = max(1, len(rows[0]))
    allowed = max(1, min(_PHRASE_ROW_BUDGET, _PHRASE_CELL_BUDGET // width))
    return rows[:allowed], total


def _phrase_instruction(shown: int, total: int) -> str:
    if shown < total:
        return (
            f"You are seeing {shown} of {total} rows - the rest were withheld to "
            "keep this request small, and the full set is displayed in a table "
            "beside your answer. Describe what the sample shows and say plainly "
            f"that it is a sample of {total} rows. Do not extrapolate totals. Use "
            "figures from the rows only."
        )
    if total > _ENUMERATE_LIMIT:
        return (
            f"Those {total} rows are the complete result set and are already "
            "displayed in a table beside your answer. Do NOT list them all. Give "
            "a short summary: the headline total or range, the top few and bottom "
            "few by value, and anything notable about the distribution. Use "
            "figures from the rows only."
        )
    return "Answer the question from those rows. Be brief. Use figures from the rows only."


def _source_objects() -> dict[str, str]:
    """Entity name -> physical object, read from the DAB config.

    Only used to find the statement DAB ran in Fabric's query history. DAB never
    exposes the physical object over MCP, and this lane does not either.
    """
    try:
        with open(DAB_CONFIG, encoding="utf-8") as handle:
            cfg = json.load(handle)
    except OSError:
        return {}
    out = {}
    for name, spec in (cfg.get("entities") or {}).items():
        obj = (spec.get("source") or {}).get("object")
        if obj:
            out[name] = obj
    return out


def _rows_from(payload: Any) -> list[dict]:
    """Normalise a DAB tool response into a list of row dicts.

    read_records nests rows under result.value; aggregate_records returns
    result as a bare list. Both shapes are handled rather than assumed.
    """
    result = payload.get("result") if isinstance(payload, dict) else payload
    if isinstance(result, dict):
        result = result.get("value", result.get("items", []))
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    return []


class DabLane:
    """Synchronous wrapper over DAB's async MCP endpoint.

    The app serves sync endpoints from a threadpool, so each call runs its own
    event loop on a worker thread and streams events back over a queue.
    """

    def __init__(self) -> None:
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
        self.deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4-mini")
        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            azure_ad_token_provider=get_bearer_token_provider(
                AzureCliCredential(), "https://cognitiveservices.azure.com/.default"
            ),
            api_version=_API_VERSION,
        )
        self._catalog: str | None = None
        self._tool_schemas: list[dict] | None = None
        self._server_info: dict[str, Any] = {}
        self._sources = _source_objects()
        self._lock = threading.Lock()

    # -- DAB discovery ------------------------------------------------------

    async def _discover(self) -> tuple[str, list[dict], dict[str, Any]]:
        """Ask DAB what it is and what it exposes. Cached after the first call."""
        async with streamablehttp_client(DAB_URL, timeout=60) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listing = await session.list_tools()
                catalog_raw = await session.call_tool(_CATALOG_TOOL, {})
                catalog = "".join(
                    b.text for b in catalog_raw.content if getattr(b, "type", None) == "text"
                )

        schemas = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": (t.description or "")[:1024],
                    "parameters": t.inputSchema,
                },
            }
            for t in listing.tools
            if t.name != _CATALOG_TOOL
        ]
        info = {
            "server": f"{init.serverInfo.name} {init.serverInfo.version}",
            "protocol": init.protocolVersion,
            "url": DAB_URL,
            "tools": [t.name for t in listing.tools],
        }
        return catalog, schemas, info

    def _ensure_discovered(self) -> None:
        with self._lock:
            if self._catalog is None:
                self._catalog, self._tool_schemas, self._server_info = asyncio.run(
                    self._discover()
                )

    async def _call(self, tool: str, args: dict) -> tuple[dict, float, float]:
        """Returns (payload, connect_ms, call_ms).

        The MCP handshake is timed separately from the tool call. A stateless
        client pays a fresh connect per question, and folding that into the
        query number would overstate what the database actually costs.
        """
        t_conn = time.perf_counter()
        async with streamablehttp_client(DAB_URL, timeout=300) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                connect_ms = (time.perf_counter() - t_conn) * 1000
                t0 = time.perf_counter()
                result = await session.call_tool(tool, args)
                call_ms = (time.perf_counter() - t0) * 1000
        text = "".join(
            b.text for b in result.content if getattr(b, "type", None) == "text"
        )
        try:
            return json.loads(text), connect_ms, call_ms
        except json.JSONDecodeError:
            return {"status": "error", "error": {"message": text[:600]}}, connect_ms, call_ms

    # -- the SQL DAB actually ran -------------------------------------------

    def actual_sql(self, entity: str, since_utc: str, budget_s: float = 150.0) -> dict:
        """Recover the statement DAB emitted, from Fabric's query history.

        DAB does not return its generated SQL over MCP. Fabric records every
        statement in queryinsights.exec_requests_history, so the real one is
        recoverable - but ingestion lags by up to a couple of minutes, which is
        why this is a separate call the UI makes after the answer has landed.

        Correlation is by source object plus submit time. That is sound for a
        single-user demo and would need a correlation id in a shared system.
        """
        from fabric_sql import FabricSqlClient

        obj = self._sources.get(entity)
        if not obj:
            return {"status": "unknown_entity", "entity": entity}

        bare = obj.split(".")[-1]
        client = FabricSqlClient.from_env()
        deadline = time.time() + budget_s
        while True:
            # FOR JSON PATH is DAB's signature - it wraps every read that way.
            # Excluding queryinsights keeps this lookup from matching itself, or
            # any other tool that merely mentions the view by name.
            rows = client.query(
                "SELECT TOP 1 command, submit_time FROM queryinsights.exec_requests_history "
                "WHERE command LIKE ? "
                "  AND command LIKE '%FOR JSON PATH%' "
                "  AND command NOT LIKE '%queryinsights%' "
                "  AND submit_time >= ? "
                "ORDER BY submit_time DESC",
                (f"%{bare}%", since_utc),
            ).rows
            if rows:
                return {
                    "status": "found",
                    "sql": str(rows[0][0]),
                    "submit_time": str(rows[0][1]),
                    "source": "queryinsights.exec_requests_history",
                }
            if time.time() >= deadline:
                return {
                    "status": "pending",
                    "detail": (
                        "Fabric had not yet published the statement to query "
                        "history. It usually lands within a couple of minutes."
                    ),
                }
            time.sleep(5)

    # -- core ---------------------------------------------------------------

    def _run(self, question: str, emit) -> dict[str, Any]:
        started = time.perf_counter()
        self._ensure_discovered()
        assert self._tool_schemas is not None

        emit({
            "t": "step", "id": "catalog", "state": "ok",
            "detail": {
                "source": self._server_info.get("server", "data-api-builder"),
                "protocol": self._server_info.get("protocol"),
                "url": DAB_URL,
                "tools": self._server_info.get("tools", []),
                "entities": list(self._sources),
            },
        })

        emit({"t": "step", "id": "route", "state": "run"})
        t0 = time.perf_counter()
        routed = self._client.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": _SYSTEM.format(catalog=self._catalog)},
                {"role": "user", "content": question},
            ],
            tools=self._tool_schemas,
            tool_choice="required",
            max_completion_tokens=4000,
        )
        route_ms = (time.perf_counter() - t0) * 1000

        calls = routed.choices[0].message.tool_calls or []
        if not calls:
            emit({"t": "error", "id": "route", "message": "model returned no tool call"})
            return self._fail(started, "no_tool_selected", "model returned no tool call")

        call = calls[0]
        name = call.function.name
        args = json.loads(call.function.arguments or "{}")
        emit({
            "t": "step", "id": "route", "state": "ok",
            "ms": round(route_ms), "tool": name, "args": args,
        })

        # Timestamped before the call so the query-history lookup has a floor.
        since_utc = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        emit({"t": "step", "id": "execute", "state": "run"})
        payload, connect_ms, query_ms = asyncio.run(self._call(name, args))

        if isinstance(payload, dict) and payload.get("status") == "error":
            err = (payload.get("error") or {})
            message = err.get("message") or "tool reported an error"
            emit({"t": "step", "id": "execute", "state": "fail", "ms": round(query_ms)})
            emit({
                "t": "blocked", "id": "execute",
                "message": message,
                "error_type": err.get("type"),
                "elapsed_s": time.perf_counter() - started,
            })
            return self._fail(started, "rejected", message)

        rows = _rows_from(payload)
        emit({
            "t": "step", "id": "execute", "state": "ok",
            "ms": round(query_ms), "row_count": len(rows),
            "connect_ms": round(connect_ms),
            "entity": args.get("entity"),
        })
        emit({"t": "rows", "rows": rows, "note": None})

        emit({"t": "step", "id": "phrase", "state": "run"})
        t2 = time.perf_counter()
        chunks: list[str] = []
        sample, total = _phrase_payload(rows)
        stream = self._client.chat.completions.create(
            model=self.deployment,
            messages=[
                {"role": "system", "content": _SYSTEM.format(catalog=self._catalog)},
                {"role": "user", "content": question},
                {"role": "assistant", "content": f"Tool {name} returned:\n{json.dumps(sample, default=str)}"},
                {"role": "user", "content": _phrase_instruction(len(sample), total)},
            ],
            max_completion_tokens=4000,
            stream=True,
        )
        for event in stream:
            if not event.choices:
                continue
            piece = event.choices[0].delta.content
            if piece:
                chunks.append(piece)
                emit({"t": "token", "text": piece})
        phrase_ms = (time.perf_counter() - t2) * 1000
        emit({"t": "step", "id": "phrase", "state": "ok", "ms": round(phrase_ms)})

        timing = {
            "route_ms": round(route_ms),
            "connect_ms": round(connect_ms),
            "query_ms": round(query_ms),
            "phrase_ms": round(phrase_ms),
        }
        # The UI fetches the real statement separately - Fabric publishes it to
        # query history on a lag, and holding the stream open for that would
        # freeze the lane.
        emit({
            "t": "sql_pending",
            "entity": args.get("entity"),
            "since_utc": since_utc,
        })
        emit({
            "t": "done",
            "elapsed_s": time.perf_counter() - started,
            "answer": "".join(chunks),
            "timing": timing,
        })
        return {
            "lane": "mcp",
            "status": "completed",
            "elapsed_s": time.perf_counter() - started,
            "answer": "".join(chunks),
            "tool": name,
            "args": args,
            "row_count": len(rows),
            "rows": rows,
            "timing": timing,
        }

    # -- public surface -----------------------------------------------------

    def warm(self) -> None:
        """Pay discovery and model connection cost before a demo, not during."""
        try:
            self._ensure_discovered()
            self._client.chat.completions.create(
                model=self.deployment,
                messages=[{"role": "user", "content": "OK"}],
                max_completion_tokens=2000,
            )
        except Exception:  # noqa: BLE001
            pass

    def ask(self, question: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return self._run(question, lambda _e: None)
        except Exception as exc:  # noqa: BLE001
            return self._fail(started, f"error:{type(exc).__name__}", str(exc))

    def ask_stream(self, question: str) -> Iterator[dict[str, Any]]:
        yield from self._stream(lambda emit: self._run(question, emit))

    def guardrail_stream(self, tool: str, args: dict) -> Iterator[dict[str, Any]]:
        """Send hostile arguments straight to DAB, with the model bypassed.

        The point is that DAB rejects them, not that a well-behaved model
        declines to ask. Nothing here inspects the arguments first - whatever
        comes back is DAB's own answer.
        """
        yield from self._stream(lambda emit: self._run_guardrail(tool, args, emit))

    def _run_guardrail(self, tool: str, args: dict, emit) -> dict[str, Any]:
        started = time.perf_counter()
        self._ensure_discovered()

        emit({
            "t": "step", "id": "catalog", "state": "ok",
            "detail": {
                "source": self._server_info.get("server", "data-api-builder"),
                "protocol": self._server_info.get("protocol"),
                "url": DAB_URL,
                "tools": self._server_info.get("tools", []),
                "entities": list(self._sources),
            },
        })
        emit({
            "t": "step", "id": "route", "state": "skip",
            "tool": tool, "args": args,
            "detail": "model bypassed - arguments sent straight to DAB",
        })

        emit({"t": "step", "id": "execute", "state": "run"})
        payload, connect_ms, query_ms = asyncio.run(self._call(tool, args))

        if isinstance(payload, dict) and payload.get("status") == "error":
            err = payload.get("error") or {}
            message = err.get("message") or "tool reported an error"
            emit({"t": "step", "id": "execute", "state": "fail", "ms": round(query_ms)})
            emit({"t": "step", "id": "phrase", "state": "skip"})
            emit({
                "t": "blocked",
                "message": message,
                "error_type": err.get("type"),
                "elapsed_s": time.perf_counter() - started,
            })
            return {"lane": "mcp", "status": "rejected", "error": message}

        rows = _rows_from(payload)
        emit({
            "t": "step", "id": "execute", "state": "ok",
            "ms": round(query_ms), "row_count": len(rows),
            "connect_ms": round(connect_ms),
        })
        emit({"t": "rows", "rows": rows, "note": None})
        emit({"t": "step", "id": "phrase", "state": "skip"})
        emit({
            "t": "done",
            "elapsed_s": time.perf_counter() - started,
            "answer": "",
            "timing": {"query_ms": round(query_ms)},
            "unexpected": True,
        })
        return {"lane": "mcp", "status": "completed", "row_count": len(rows)}

    @staticmethod
    def _stream(work) -> Iterator[dict[str, Any]]:
        events: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                work(events.put)
            except Exception as exc:  # noqa: BLE001
                events.put({
                    "t": "error", "id": "lane",
                    "message": f"{type(exc).__name__}: {exc}",
                })
            finally:
                events.put(_SENTINEL)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            event = events.get()
            if event is _SENTINEL:
                break
            yield event

    @staticmethod
    def _fail(started: float, status: str, detail: str) -> dict[str, Any]:
        return {
            "lane": "mcp",
            "status": status,
            "elapsed_s": time.perf_counter() - started,
            "answer": "",
            "error": detail[:600],
            "timing": {},
        }
