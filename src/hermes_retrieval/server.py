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
    "Hermes Retrieval",
    instructions=(
        "Discover and load skills only when needed, or retrieve semantically relevant "
        "prior context. Chroma is a cache; returned locators identify canonical sources."
    ),
)


@lru_cache(maxsize=1)
def _service() -> RetrievalService:
    return RetrievalService()


@mcp.tool()
def find_skills(query: str, limit: int = 8) -> dict:
    """Find applicable skills by meaning without loading their instructions."""
    return _service().find_skills(query=query, limit=limit)


@mcp.tool()
def load_skills(skill_ids: list[str]) -> dict:
    """Load the canonical SKILL.md files for one or more selected skill IDs."""
    return _service().load_skills(skill_ids=skill_ids)


@mcp.tool()
def recall(query: str, sources: list[str] | None = None, limit: int = 8) -> dict:
    """Retrieve relevant prior context from sessions, context-mode, or Librarian."""
    return _service().recall(query=query, sources=sources, limit=limit)


@mcp.tool()
def sync_sources(sources: list[str] | None = None) -> dict:
    """Refresh explicitly configured canonical sources into the disposable index."""
    return _service().sync(names=sources)


@mcp.tool()
def retrieval_status() -> dict:
    """Report configured sources, embedding lanes, and Chroma index counts."""
    return _service().status()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
