"""Thin client for the reviewer-action MCP server (mcp_review, the only writable lane).

Used by the AG-UI REST layer (human clicks) and by run_review (to read reviewer-supplied data).
The LLM agents get NO tools here: dispositions are made by people, not by the model."""
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REVIEW_MCP = os.environ.get("REVIEW_MCP_URL", "http://host.docker.internal:8102/mcp")
SQLITE_MCP = os.environ.get("SQLITE_MCP_URL", "http://host.docker.internal:8101/mcp")
_HEADERS = lambda: {"Authorization": f"Bearer {os.environ.get('MCP_API_TOKEN', '')}"}


class ReviewError(Exception):
    """The review server rejected the call (validation) or is unreachable."""


async def call_review(tool: str, args: dict, timeout: float = 15.0):
    return await _call(REVIEW_MCP, tool, args, timeout)


async def call_sqlite(tool: str, args: dict, timeout: float = 15.0):
    """Read-only case data (used to verify the model's citations against the real record)."""
    return await _call(SQLITE_MCP, tool, args, timeout)


async def _call(url: str, tool: str, args: dict, timeout: float):
    import asyncio

    async def _go():
        async with streamablehttp_client(url, headers=_HEADERS()) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool, args)
                text = "".join(c.text for c in res.content if getattr(c, "text", None))
                if res.isError:
                    raise ReviewError(text.split(": ", 1)[-1] if text else "MCP server error")
                try:
                    return json.loads(text)
                except Exception:
                    return {"raw": text}

    try:
        return await asyncio.wait_for(_go(), timeout)
    except ReviewError:
        raise
    except Exception as e:  # unreachable / timeout: say so, never pretend it was saved
        raise ReviewError(f"service unavailable: {type(e).__name__}") from e
