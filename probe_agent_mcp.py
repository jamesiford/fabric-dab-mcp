"""Probe the Fabric data agent's native MCP endpoint.

The OpenAI-assistants surface on this agent failed 45/45 times with BadRequest.
Fabric also publishes each *published* data agent as an
MCP server over streamable HTTP. This script speaks real MCP to that endpoint:
initialize -> tools/list -> tools/call.

Docs: /fabric/data-science/data-agent-mcp-server
URL:  https://api.fabric.microsoft.com/v1/mcp/workspaces/{ws}/dataagents/{id}/agent
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from azure.identity import AzureCliCredential
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

WORKSPACE_ID = os.environ["FABRIC_WORKSPACE_ID"]
DATA_AGENT_ID = os.environ["FABRIC_DATA_AGENT_ID"]
SCOPE = "https://api.fabric.microsoft.com/.default"

MCP_URL = (
    f"https://api.fabric.microsoft.com/v1/mcp/workspaces/{WORKSPACE_ID}"
    f"/dataagents/{DATA_AGENT_ID}/agent"
)

QUESTION = "What is the total deposit balance by branch?"


async def main() -> None:
    cred = AzureCliCredential()
    tok = cred.get_token(SCOPE)
    headers = {"Authorization": f"Bearer {tok.token}"}

    print(f"URL: {MCP_URL}\n")

    t0 = time.perf_counter()
    async with streamablehttp_client(MCP_URL, headers=headers, timeout=180) as (r, w, _):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            print(f"initialize OK in {time.perf_counter() - t0:.2f}s")
            print(f"  server: {init.serverInfo.name} {init.serverInfo.version}")
            print(f"  protocol: {init.protocolVersion}\n")

            tools = await session.list_tools()
            print(f"tools/list -> {len(tools.tools)} tool(s)")
            for t in tools.tools:
                print(f"  name: {t.name}")
                print(f"  desc: {(t.description or '')[:400]}")
                print(f"  schema: {json.dumps(t.inputSchema)[:400]}\n")

            tool = tools.tools[0]
            arg = next(iter(tool.inputSchema["properties"]))

            print(f"tools/call {tool.name}({arg}=...)")
            print(f"  q: {QUESTION}")
            t1 = time.perf_counter()
            result = await session.call_tool(tool.name, {arg: QUESTION})
            elapsed = time.perf_counter() - t1

            print(f"  -> {elapsed:.2f}s  isError={result.isError}")
            for block in result.content:
                if block.type == "text":
                    print("\n--- answer ---")
                    print(block.text[:4000])
                else:
                    print(f"  [{block.type} block]")


if __name__ == "__main__":
    asyncio.run(main())
