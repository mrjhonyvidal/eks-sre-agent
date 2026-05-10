"""
HTTP shim that bridges Slack bot HTTP calls to an `awslabs/mcp` server
running as a stdio subprocess.

Endpoint contract (matches `eks_ai_ops.interactive.mcp_tools.MCPToolClient`):

    POST /tools/call
    Authorization: Bearer <MCP_GATEWAY_API_KEY>   (optional)
    {
      "server":   "eks",
      "tool":     "list_pods" | "describe_resource" | "get_logs" | ...,
      "arguments": { ... }
    }

The shim spawns one MCP stdio child per server name on first use and
reuses it for the lifetime of the process. The child is the official
`awslabs.eks-mcp-server` (or any other MCP server you configure in
`MCP_SERVERS`, comma-separated `name=command`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("mcp-gateway")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# Format: "name1=command with args,name2=other command"
# Default: a single "eks" server pointing at awslabs eks-mcp-server.
DEFAULT_SERVERS = "eks=python -m awslabs.eks_mcp_server"
SERVERS_RAW = os.environ.get("MCP_SERVERS", DEFAULT_SERVERS)
API_KEY = os.environ.get("MCP_GATEWAY_API_KEY", "").strip()


class ToolCall(BaseModel):
    server: str = Field(..., description="Logical MCP server name, e.g. 'eks'")
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class StdioMCPClient:
    """Minimal JSON-RPC 2.0 client over stdio for an MCP server subprocess."""

    def __init__(self, name: str, command: str) -> None:
        self.name = name
        self.command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._lock = asyncio.Lock()
        self._initialized = False

    async def start(self) -> None:
        argv = shlex.split(self.command)
        logger.info("Starting MCP server %r: %s", self.name, argv)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "eks-ai-ops-mcp-gateway", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized", {})
        self._initialized = True

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            await self.start()
        return await self._rpc("tools/call", {"name": tool, "arguments": arguments})

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            assert self._proc and self._proc.stdin and self._proc.stdout
            self._req_id += 1
            req = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
            self._proc.stdin.write((json.dumps(req) + "\n").encode())
            await self._proc.stdin.drain()
            line = await self._proc.stdout.readline()
            if not line:
                raise RuntimeError(f"MCP server {self.name!r} closed stdout")
            msg = json.loads(line.decode())
            if "error" in msg:
                raise RuntimeError(f"MCP error: {msg['error']}")
            return msg.get("result", {})

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        assert self._proc and self._proc.stdin
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()


def _parse_servers(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, cmd = chunk.partition("=")
        if name and cmd:
            out[name.strip()] = cmd.strip()
    return out


_clients: dict[str, StdioMCPClient] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for name, cmd in _parse_servers(SERVERS_RAW).items():
        _clients[name] = StdioMCPClient(name, cmd)
    try:
        yield
    finally:
        for c in _clients.values():
            await c.stop()


app = FastAPI(title="eks-ai-ops MCP gateway", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "servers": ",".join(_clients.keys())}


@app.post("/tools/call")
async def tools_call(request: Request, body: ToolCall) -> dict[str, Any]:
    if API_KEY:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {API_KEY}":
            raise HTTPException(status_code=401, detail="unauthorized")
    client = _clients.get(body.server)
    if not client:
        raise HTTPException(status_code=404, detail=f"unknown server {body.server!r}")
    try:
        return await client.call_tool(body.tool, body.arguments)
    except Exception as exc:
        logger.exception("Tool call failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
