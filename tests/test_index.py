from pathlib import Path
from types import SimpleNamespace

from hermes_retrieval.index import RetrievalIndex
from hermes_retrieval.models import SourceConfig


class _Collection:
    def __init__(self, count: int):
        self._count = count

    def count(self) -> int:
        return self._count


class _Client:
    def __init__(self, collections: dict[str, _Collection]):
        self.collections = collections
        self.deleted: list[str] = []

    def get_collection(self, name: str) -> _Collection:
        if name not in self.collections:
            raise KeyError(name)
        return self.collections[name]

    def delete_collection(self, name: str) -> None:
        self.deleted.append(name)
        self.collections.pop(name)


def test_prune_unmanaged_deletes_disabled_and_native_collections() -> None:
    index = object.__new__(RetrievalIndex)
    index.embedders = [SimpleNamespace(lane="fastembed")]
    disabled_name = "hermes-retrieval-context_mode-history-fastembed"
    enabled_name = "hermes-retrieval-skills-library-fastembed"
    native_name = "hermes-retrieval-skills-active-fastembed"
    index.client = _Client(
        {
            disabled_name: _Collection(42),
            enabled_name: _Collection(7),
            native_name: _Collection(3),
        }
    )
    sources = [
        SourceConfig("history", "context_mode", Path("/history"), False),
        SourceConfig("library", "skills", Path("/library"), True),
        SourceConfig("active", "skills", Path("/active"), True, "native"),
    ]

    report = index.prune_unmanaged(sources)

    assert index.client.deleted == [disabled_name, native_name]
    assert enabled_name in index.client.collections
    assert report == [
        {
            "source": "history",
            "kind": "context_mode",
            "lane": "fastembed",
            "collection": disabled_name,
            "documents": 42,
            "reason": "disabled",
        },
        {
            "source": "active",
            "kind": "skills",
            "lane": "fastembed",
            "collection": native_name,
            "documents": 3,
            "reason": "native_catalog_only",
        }
    ]
