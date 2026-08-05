from pathlib import Path

from hermes_retrieval.context_audit import audit_context


def test_context_audit_is_read_only_and_reports_drift_imports_and_shadowing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    native = repo / ".omp" / "AGENTS.md"
    standalone = repo / "AGENTS.md"
    reference = repo / "docs" / "history.md"
    baseline = tmp_path / "baseline"
    baseline_native = baseline / ".omp" / "AGENTS.md"
    native.parent.mkdir(parents=True)
    reference.parent.mkdir(parents=True)
    baseline_native.parent.mkdir(parents=True)
    native.write_text(
        "# Rules\n\nRun tests.\n\n# History\n\nRead @../docs/history.md.\n",
        encoding="utf-8",
    )
    standalone.write_text("# Fallback\n\nOld rules.\n", encoding="utf-8")
    reference.write_text("Long history.\n", encoding="utf-8")
    baseline_native.write_text("# Rules\n\nRun tests.\n", encoding="utf-8")
    before = native.read_bytes()

    report = audit_context([repo], baseline=baseline)

    assert report["read_only"] is True
    assert native.read_bytes() == before
    native_row = next(row for row in report["files"] if row["path"] == str(native))
    standalone_row = next(row for row in report["files"] if row["path"] == str(standalone))
    assert native_row["baseline"]["status"] == "changed"
    assert native_row["imports"][0]["exists"] is True
    assert standalone_row["potential_shadowed_by"] == [str(native)]
