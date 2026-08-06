from __future__ import annotations

from functools import lru_cache
import logging
import sys

from mcp.server.fastmcp import FastMCP

from .service import RetrievalService

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP(
    "Retrieval",
    instructions=(
        "Retrieve at most one specialist skill through an isolated read-only scout. "
        "The exact SKILL.md is returned immediately and its complete package is copied "
        "only into this MCP instance's Hermes or OMP projection lane. Optional reference "
        "libraries are retrieved separately as bounded heading sections. Projection "
        "inspection and cleanup are intentionally human-only CLI operations."
    ),
)


@lru_cache(maxsize=1)
def _service() -> RetrievalService:
    return RetrievalService()


@mcp.tool()
def retrieve_skill(query: str) -> dict:
    """Find, inspect, and temporarily project at most one relevant dormant skill.

    Selection runs in an ephemeral OMP RPC process with all native tools, skills,
    rules, and extensions disabled. It can call only Retrieval's read-only search
    and graph-read tools. The selected SKILL.md is returned verbatim now; the
    package projection persists for later compaction/reload.
    """

    return _service().retrieve_skill(query=query)


@mcp.tool()
def retrieve_reference(
    query: str,
    limit: int = 3,
    max_chars: int = 8000,
) -> dict:
    """Retrieve optional reference material as bounded Markdown heading sections.

    Reference sources are explicitly configured and read-only. Active AGENTS,
    RULES, session context, and tool output are not mirrored automatically.
    """

    return _service().retrieve_reference(
        query=query,
        limit=limit,
        max_chars=max_chars,
    )


def main() -> None:
    service = _service()
    service.start_watcher()
    try:
        mcp.run(transport="stdio")
    finally:
        service.stop_watcher()


if __name__ == "__main__":
    main()
