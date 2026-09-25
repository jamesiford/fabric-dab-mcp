"""Fabric data agent lane - the 'before' side of the side-by-side.

Talks to the data agent over its **native MCP endpoint**, which is the surface
that actually works:

    https://api.fabric.microsoft.com/v1/mcp/workspaces/{ws}/dataagents/{id}/agent

History, so the measurement is read honestly. This lane previously used the
OpenAI-assistants surface Fabric exposes at `/aiskills/{id}/aiassistant/openai`.
That surface failed 45 out of 45 attempts with `BadRequest` on `threads/{id}/runs`
across three sessions on clean threads. The MCP endpoint answered correctly on
the first attempt, so the comparison is measurable.

Two things to keep in mind when reading the numbers this produces:

1.  The data agent is a black box. There is no route/build/query/phrase
    breakdown available, because planning, generation and execution all happen
    inside Fabric. What we can time is the handshake, the tool discovery and the
    call itself. That opacity is part of what is being compared.
2.  The answer arrives as one block. This lane does not fake token streaming to
    match the MCP lane's cadence - it emits exactly one chunk, because exactly
    one chunk is what Fabric returns.

Per the Microsoft docs, the tool name and its question argument are discovered
from the advertised input schema rather than hard-coded, so this keeps working
if Fabric renames either.
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
from typing import Any, Iterator

from azure.identity import AzureCliCredential
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

WORKSPACE_ID = os.environ.get("FABRIC_WORKSPACE_ID", "").strip()
DATA_AGENT_ID = os.environ.get("FABRIC_DATA_AGENT_ID", "").strip()
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
CALL_TIMEOUT_S = float(os.environ.get("DATA_AGENT_TIMEOUT_S", "300"))

MCP_URL = (
    f"https://api.fabric.microsoft.com/v1/mcp/workspaces/{WORKSPACE_ID}"
    f"/dataagents/{DATA_AGENT_ID}/agent"
)

_SENTINEL = object()


class DataAgentLane:
    """Synchronous wrapper over the data agent's async MCP endpoint.

    The app serves sync endpoints from a threadpool, so each call runs its own
    event loop on a worker thread. Streaming events cross back over a queue,
    which lets the trace panel show the handshake and the call as they land
    rather than only after the whole turn finishes.
    """

    def __init__(self) -> None:
        self._credential = AzureCliCredential()
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._lock = threading.Lock()

    # -- auth ---------------------------------------------------------------

    def _bearer(self) -> str:
        """Cached Fabric token. Refreshed two minutes before expiry."""
        with self._lock:
            now = time.time()
            if self._token is None or now >= self._token_expires - 120:
                tok = self._credential.get_token(FABRIC_SCOPE)
                self._token = tok.token
                self._token_expires = tok.expires_on
            return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer()}"}

    # -- core ---------------------------------------------------------------

    async def _run(self, question: str, emit) -> dict[str, Any]:
        if not (WORKSPACE_ID and DATA_AGENT_ID):
            raise RuntimeError("set FABRIC_WORKSPACE_ID and FABRIC_DATA_AGENT_ID to use the data agent lane")
        started = time.perf_counter()

        emit({"t": "step", "id": "connect", "state": "run"})
        t0 = time.perf_counter()
        async with streamablehttp_client(
            MCP_URL, headers=self._headers(), timeout=CALL_TIMEOUT_S
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                connect_ms = (time.perf_counter() - t0) * 1000
                emit({
                    "t": "step", "id": "connect", "state": "ok",
                    "ms": round(connect_ms),
                    "detail": {
                        "server": f"{init.serverInfo.name} {init.serverInfo.version}",
                        "protocol": init.protocolVersion,
                        "url": MCP_URL,
                    },
                })

                emit({"t": "step", "id": "discover", "state": "run"})
                t1 = time.perf_counter()
                listing = await session.list_tools()
                discover_ms = (time.perf_counter() - t1) * 1000

                if not listing.tools:
                    raise RuntimeError("data agent advertised no tools")

                tool = listing.tools[0]
                # Discovered, not hard-coded - Fabric may rename either.
                arg = next(iter(tool.inputSchema.get("properties", {})), None)
                if arg is None:
                    raise RuntimeError(f"tool '{tool.name}' exposes no arguments")

                emit({
                    "t": "step", "id": "discover", "state": "ok",
                    "ms": round(discover_ms), "tool": tool.name,
                    "detail": {
                        "tool_count": len(listing.tools),
                        "argument": arg,
                        "description": (tool.description or "")[:300],
                    },
                })

                emit({"t": "step", "id": "agent", "state": "run"})
                t2 = time.perf_counter()
                result = await session.call_tool(tool.name, {arg: question})
                agent_ms = (time.perf_counter() - t2) * 1000

                answer = "\n".join(
                    block.text for block in result.content
                    if getattr(block, "type", None) == "text"
                )

                if result.isError:
                    emit({"t": "step", "id": "agent", "state": "fail",
                          "ms": round(agent_ms)})
                    emit({"t": "error", "id": "agent",
                          "message": answer[:600] or "tool reported an error"})
                    return {
                        "lane": "data_agent", "status": "tool_error",
                        "elapsed_s": time.perf_counter() - started,
                        "answer": "", "error": answer[:600],
                        "tool": tool.name,
                        "timing": {
                            "connect_ms": round(connect_ms),
                            "discover_ms": round(discover_ms),
                            "agent_ms": round(agent_ms),
                        },
                    }

                emit({"t": "step", "id": "agent", "state": "ok",
                      "ms": round(agent_ms)})
                # One chunk, because one chunk is what Fabric returns.
                emit({"t": "token", "text": answer})

                payload = {
                    "lane": "data_agent",
                    "status": "completed",
                    "elapsed_s": time.perf_counter() - started,
                    "answer": answer,
                    "tool": tool.name,
                    "timing": {
                        "connect_ms": round(connect_ms),
                        "discover_ms": round(discover_ms),
                        "agent_ms": round(agent_ms),
                    },
                }
                emit({
                    "t": "done",
                    "elapsed_s": payload["elapsed_s"],
                    "answer": answer,
                    "timing": payload["timing"],
                })
                return payload

    # -- public surface -----------------------------------------------------

    def warm(self) -> None:
        """Pay the token and handshake cost before a demo, not during one."""
        try:
            self._bearer()
            asyncio.run(self._run("How many deposit accounts are there?", lambda _e: None))
        except Exception:  # noqa: BLE001
            pass

    def ask(self, question: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            return asyncio.run(self._run(question, lambda _e: None))
        except Exception as exc:  # noqa: BLE001
            return self._fail(started, f"error:{type(exc).__name__}", str(exc))

    def ask_stream(self, question: str) -> Iterator[dict[str, Any]]:
        """Same work as ask(), with each MCP stage emitted as it completes."""
        events: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                asyncio.run(self._run(question, events.put))
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
            "lane": "data_agent",
            "status": status,
            "elapsed_s": time.perf_counter() - started,
            "answer": "",
            "error": detail[:600],
            "timing": {},
        }


# Backwards-compatible alias: bench.py and older scripts imported this name.
DataAgentClient = DataAgentLane
