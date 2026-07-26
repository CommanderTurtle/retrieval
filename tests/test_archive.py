from pathlib import Path

from hermes_retrieval.archive import ArchiveStore
from hermes_retrieval.models import Document, SourceConfig


def _document(record_id: str, position: int, content: str) -> Document:
    return Document(
        record_id=record_id,
        source_name="sessions",
        kind="hermes_sessions",
        title="Session",
        content=content,
        locator=f"session:{record_id}",
        metadata={
            "session_id": "one",
            "message_position": position,
            "chunk_index": 0,
            "timestamp": float(position),
        },
    )


def test_archive_retains_pruned_events_and_orders_neighbors(tmp_path: Path):
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    source = SourceConfig("sessions", "hermes_sessions", Path("/unused"))
    store.sync(
        source,
        [_document("a", 0, "first"), _document("b", 1, "second")],
        retain_history=True,
    )
    report = store.sync(
        source,
        [_document("b", 1, "second"), _document("c", 2, "third")],
        retain_history=True,
    )
    assert report == {"observed": 2, "archived": 3, "historical": 1}
    assert [item.record_id for item in store.neighbors("b", 1, 1)] == ["a", "b", "c"]
