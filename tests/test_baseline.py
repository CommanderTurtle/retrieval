from pathlib import Path

from hermes_retrieval.baseline import _ROUTER_PATHS


def test_managed_router_allowlist_is_exact() -> None:
    assert _ROUTER_PATHS == {
        "hermes": (
            Path("research/retrieve-knowledge"),
            Path("web/localflame"),
            Path("research/librarian"),
        ),
        "omp": (
            Path("retrieve-knowledge"),
            Path("localflame"),
            Path("librarian"),
        ),
    }
