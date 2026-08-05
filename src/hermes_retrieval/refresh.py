from __future__ import annotations

from collections.abc import Iterable
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import select
import struct
import threading
import time
from typing import TYPE_CHECKING, Any

from filelock import FileLock, Timeout as FileLockTimeout

from .models import SourceConfig
from .sources import (
    _WORKFLOW_DIRECTORIES,
    _is_workflow_file,
    _iter_skill_paths,
)

if TYPE_CHECKING:
    from .service import RetrievalService

logger = logging.getLogger(__name__)

_IGNORED_DIRECTORIES = {
    ".git",
    ".pytest_cache",
    ".retrieval-archive",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}

_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ISDIR = 0x40000000
_INOTIFY_MASK = (
    _IN_MODIFY
    | _IN_ATTRIB
    | _IN_CLOSE_WRITE
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
)
_INOTIFY_EVENT = struct.Struct("iIII")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def watcher_state_path(settings: Any) -> Path:
    return Path(f"{settings.sync_lock_path}.watch.state.json")


def external_watcher_snapshot(settings: Any) -> dict[str, Any] | None:
    """Read the leader watcher's cross-process heartbeat, if it is current."""

    path = watcher_state_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        updated_at_epoch = float(payload["updated_at_epoch"])
        pid = int(payload["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    max_age = max(
        10.0,
        float(settings.watch_poll_seconds) * 2.0
        + float(settings.watch_debounce_ms) / 1000.0,
    )
    age = max(0.0, time.time() - updated_at_epoch)
    process_alive = True
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        process_alive = False
    snapshot = {
        key: value
        for key, value in payload.items()
        if key not in {"updated_at_epoch"}
    }
    snapshot.update(
        {
            "external": True,
            "heartbeat_age_seconds": round(age, 3),
            "state_file": str(path),
            "healthy": bool(
                payload.get("healthy")
                and payload.get("leader")
                and process_alive
                and age <= max_age
            ),
        }
    )
    if not snapshot["healthy"]:
        snapshot["last_error"] = (
            str(snapshot.get("last_error") or "")
            or "External watcher heartbeat is stale or its process exited."
        )
    return snapshot


def _hash_file(hasher: Any, logical_path: str, path: Path) -> None:
    hasher.update(logical_path.encode("utf-8", errors="surrogateescape"))
    hasher.update(b"\0")
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        hasher.update(f"<unavailable:{type(exc).__name__}>".encode("ascii"))
    hasher.update(b"\0")


def _hash_stat(hasher: Any, logical_path: str, path: Path) -> None:
    hasher.update(logical_path.encode("utf-8", errors="surrogateescape"))
    hasher.update(b"\0")
    try:
        stat = path.stat()
        value = (
            f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:"
            f"{stat.st_mtime_ns}:{stat.st_ctime_ns}"
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        value = f"<unavailable:{type(exc).__name__}>"
    hasher.update(value.encode("ascii"))
    hasher.update(b"\0")


def _logical_path(path: Path, root: Path) -> str:
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        return str(path.absolute())


def _workflow_paths(source: SourceConfig) -> Iterable[Path]:
    for directory in _WORKFLOW_DIRECTORIES:
        root = source.path / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if _is_workflow_file(path):
                yield path.resolve()


def _database_paths(source: SourceConfig) -> list[Path]:
    if source.kind == "context_mode":
        databases = sorted(source.path.glob("*.db"))
        paths = list(databases)
        paths.extend(
            Path(f"{database}-wal")
            for database in databases
            if Path(f"{database}-wal").exists()
        )
        return sorted(paths)
    if source.kind == "hermes_sessions":
        paths = []
        candidates = [source.path]
        profiles = source.path / "profiles"
        if profiles.is_dir():
            candidates.extend(
                path
                for path in profiles.iterdir()
                if path.is_dir() and not path.is_symlink()
            )
        for root in candidates:
            for relative in (
                "state.db",
                "state.db-wal",
                "sessions.json",
                "sessions/sessions.json",
                "plugins/hermes-context-mode/metrics.db",
                "plugins/hermes-context-mode/metrics.db-wal",
            ):
                path = root / relative
                if path.exists():
                    paths.append(path)
        return sorted(paths)
    return []


def source_fingerprint(source: SourceConfig) -> str:
    """Fingerprint canonical source content without querying the vector index."""
    hasher = hashlib.sha256()
    hasher.update(
        f"v4\0{source.name}\0{source.kind}\0{source.state}\0{source.path}\0".encode(
            "utf-8",
            errors="surrogateescape",
        )
    )
    if not source.path.exists():
        hasher.update(b"<source-missing>")
        return hasher.hexdigest()
    if source.kind == "skills":
        for logical in _iter_skill_paths(source.path):
            canonical = logical.resolve()
            _hash_file(
                hasher,
                _logical_path(logical, source.path),
                canonical,
            )
    elif source.kind == "workflows":
        for path in _workflow_paths(source):
            _hash_file(hasher, _logical_path(path, source.path), path)
    elif source.kind == "references":
        for path in sorted(source.path.rglob("*.md")):
            if path.is_file() and not any(
                part in _IGNORED_DIRECTORIES for part in path.relative_to(source.path).parts
            ):
                _hash_file(hasher, _logical_path(path, source.path), path)
    elif source.kind in {"context_mode", "hermes_sessions"}:
        for path in _database_paths(source):
            _hash_stat(hasher, _logical_path(path, source.path), path)
    else:
        raise ValueError(f"unsupported source kind: {source.kind}")
    return hasher.hexdigest()


def _event_relevant(source: SourceConfig, path: Path) -> bool:
    if any(part in _IGNORED_DIRECTORIES for part in path.parts):
        return False
    if source.kind == "skills":
        return path.name == "SKILL.md"
    if source.kind == "workflows":
        return (
            path.suffix.lower() in {
                ".bash",
                ".json",
                ".md",
                ".sh",
                ".toml",
                ".yaml",
                ".yml",
            }
            and any(part in _WORKFLOW_DIRECTORIES for part in path.parts)
            and not path.name.endswith(("-test.sh", "_test.sh"))
        )
    if source.kind == "context_mode":
        return path.name.endswith(".db") or path.name.endswith(".db-wal")
    if source.kind == "hermes_sessions":
        return path.name in {
            "state.db",
            "state.db-wal",
            "sessions.json",
            "metrics.db",
            "metrics.db-wal",
        }
    if source.kind == "references":
        return path.suffix.casefold() == ".md"
    return False


def _watch_roots(source: SourceConfig) -> list[tuple[Path, bool]]:
    roots: list[tuple[Path, bool]] = []
    if source.kind == "skills":
        roots.append((source.path, True))
    elif source.kind == "workflows":
        roots.append((source.path, False))
        roots.extend(
            (source.path / directory, True)
            for directory in _WORKFLOW_DIRECTORIES
            if (source.path / directory).is_dir()
        )
    elif source.kind == "context_mode":
        roots.append((source.path, False))
    elif source.kind == "hermes_sessions":
        roots.append((source.path, False))
        sessions = source.path / "sessions"
        if sessions.is_dir():
            roots.append((sessions, False))
        plugin = source.path / "plugins" / "hermes-context-mode"
        if plugin.is_dir():
            roots.append((plugin, False))
        profiles = source.path / "profiles"
        if profiles.is_dir():
            roots.append((profiles, True))
    elif source.kind == "references":
        roots.append((source.path, True))
    if source.kind == "skills" and source.path.is_dir():
        for logical in _iter_skill_paths(source.path):
            canonical_parent = logical.resolve().parent
            try:
                canonical_parent.relative_to(source.path.resolve())
            except ValueError:
                roots.append((canonical_parent, False))
    if not source.path.exists():
        # Optional configured sources may be cloned after the MCP starts.
        # Watch the nearest existing parent just long enough to notice their
        # exact root appearing; refresh_source() then installs the real tree.
        parent = source.path.parent
        while parent != parent.parent and not parent.exists():
            parent = parent.parent
        if parent.is_dir():
            roots.append((parent, False))
    unique: dict[Path, bool] = {}
    for path, recursive in roots:
        path = path.absolute()
        unique[path] = unique.get(path, False) or recursive
    return sorted(unique.items(), key=lambda item: str(item[0]))


class _InotifyMonitor:
    name = "inotify"

    def __init__(self, sources: list[SourceConfig]) -> None:
        if os.name != "posix" or not hasattr(os, "O_NONBLOCK"):
            raise OSError("native inotify is unavailable on this platform")
        library = ctypes.CDLL(None, use_errno=True)
        init = getattr(library, "inotify_init1", None)
        add_watch = getattr(library, "inotify_add_watch", None)
        if init is None or add_watch is None:
            raise OSError("libc does not expose inotify")
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add_watch.restype = ctypes.c_int
        descriptor = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if descriptor < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        self.fd = descriptor
        self._add_watch = add_watch
        self.sources = {source.name: source for source in sources}
        self._wd_path: dict[int, Path] = {}
        self._path_wd: dict[Path, int] = {}
        self._wd_sources: dict[int, set[str]] = {}
        self._wd_recursive: dict[int, set[str]] = {}
        try:
            for source in sources:
                for path, recursive in _watch_roots(source):
                    self._add_tree(path, source.name, recursive)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _ignored(path: Path) -> bool:
        return any(part in _IGNORED_DIRECTORIES for part in path.parts)

    def _add_directory(
        self,
        path: Path,
        source_name: str,
        recursive: bool,
    ) -> None:
        if self._ignored(path) or not path.is_dir():
            return
        absolute = path.absolute()
        known = self._path_wd.get(absolute)
        if known is None:
            descriptor = self._add_watch(
                self.fd,
                os.fsencode(absolute),
                _INOTIFY_MASK,
            )
            if descriptor < 0:
                error = ctypes.get_errno()
                if error in {2, 13, 28}:
                    logger.warning(
                        "Skipping Retrieval watch path %s: %s",
                        absolute,
                        os.strerror(error),
                    )
                    return
                raise OSError(error, os.strerror(error), str(absolute))
            self._path_wd[absolute] = descriptor
            self._wd_path[descriptor] = absolute
            self._wd_sources[descriptor] = set()
            self._wd_recursive[descriptor] = set()
            known = descriptor
        self._wd_sources[known].add(source_name)
        if recursive:
            self._wd_recursive[known].add(source_name)

    def _add_tree(self, root: Path, source_name: str, recursive: bool) -> None:
        if not root.is_dir():
            return
        self._add_directory(root, source_name, recursive)
        if not recursive:
            return
        for current, directories, _files in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES
            ]
            self._add_directory(current_path, source_name, True)

    def refresh_source(self, source_name: str) -> None:
        source = self.sources[source_name]
        for path, recursive in _watch_roots(source):
            self._add_tree(path, source_name, recursive)

    def read(self, timeout: float) -> tuple[set[str], bool]:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return set(), False
        try:
            payload = os.read(self.fd, 1024 * 1024)
        except BlockingIOError:
            return set(), False
        changed: set[str] = set()
        overflow = False
        offset = 0
        while offset + _INOTIFY_EVENT.size <= len(payload):
            wd, mask, _cookie, name_size = _INOTIFY_EVENT.unpack_from(
                payload,
                offset,
            )
            offset += _INOTIFY_EVENT.size
            raw_name = payload[offset:offset + name_size]
            offset += name_size
            name = os.fsdecode(raw_name.split(b"\0", 1)[0])
            if mask & _IN_Q_OVERFLOW:
                overflow = True
                continue
            base = self._wd_path.get(wd)
            if base is None:
                continue
            source_names = set(self._wd_sources.get(wd, set()))
            path = base / name if name else base
            if mask & _IN_ISDIR and mask & (_IN_CREATE | _IN_MOVED_TO):
                for source_name in self._wd_recursive.get(wd, set()):
                    self._add_tree(path, source_name, True)
            if mask & _IN_ISDIR and mask & (
                _IN_CREATE | _IN_DELETE | _IN_MOVED_FROM | _IN_MOVED_TO
            ):
                for source_name in source_names:
                    if self.sources[source_name].kind in {
                        "context_mode",
                        "hermes_sessions",
                        "skills",
                        "workflows",
                    }:
                        changed.add(source_name)
            if mask & (_IN_DELETE_SELF | _IN_MOVE_SELF):
                changed.update(source_names)
            elif not (mask & _IN_ISDIR):
                for source_name in source_names:
                    if _event_relevant(self.sources[source_name], path):
                        changed.add(source_name)
            if mask & _IN_IGNORED:
                self._path_wd.pop(base, None)
                self._wd_path.pop(wd, None)
                self._wd_sources.pop(wd, None)
                self._wd_recursive.pop(wd, None)
        return changed, overflow

    def close(self) -> None:
        descriptor = getattr(self, "fd", -1)
        if descriptor >= 0:
            os.close(descriptor)
            self.fd = -1


class _PollingMonitor:
    name = "fingerprint-poll"

    def __init__(self, sources: list[SourceConfig], interval: float) -> None:
        self.sources = {source.name: source for source in sources}
        self.interval = interval
        self._fingerprints = {
            source.name: source_fingerprint(source)
            for source in sources
        }
        self._next_poll = time.monotonic() + interval

    def refresh_source(self, source_name: str) -> None:
        self._fingerprints[source_name] = source_fingerprint(
            self.sources[source_name]
        )

    def read(self, timeout: float) -> tuple[set[str], bool]:
        time.sleep(max(0.0, timeout))
        if time.monotonic() < self._next_poll:
            return set(), False
        self._next_poll = time.monotonic() + self.interval
        changed = set()
        for name, source in self.sources.items():
            fingerprint = source_fingerprint(source)
            if self._fingerprints.get(name) != fingerprint:
                changed.add(name)
                self._fingerprints[name] = fingerprint
        return changed, False

    def close(self) -> None:
        return None


class SourceRefreshWatcher:
    """Debounced source watcher with one cross-process writer in the service."""

    def __init__(self, service: RetrievalService) -> None:
        self.service = service
        self.settings = service.settings
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: set[str] = set()
        self._stale: set[str] = set()
        self._last_event_monotonic = 0.0
        self._retry_at = 0.0
        self._state: dict[str, Any] = {
            "enabled": self.settings.watch_enabled,
            "backend": "disabled" if not self.settings.watch_enabled else "starting",
            "healthy": not self.settings.watch_enabled,
            "started_at": "",
            "last_event_at": "",
            "last_reconcile_at": "",
            "last_sync_at": "",
            "last_sync_sources": [],
            "last_error": "",
            "sync_in_progress": False,
            "lock_contentions": 0,
            "leader": False,
        }
        self._last_state_write = 0.0

    def start(self) -> None:
        if not self.settings.watch_enabled:
            return
        with self._mutex:
            if self._thread and self._thread.is_alive():
                return
            self._state["started_at"] = _utc_now()
            self._thread = threading.Thread(
                target=self._run,
                name="hermes-retrieval-refresh",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        self._remove_published_state()

    def snapshot(self) -> dict[str, Any]:
        with self._mutex:
            return {
                **self._state,
                "pending_sources": sorted(self._pending),
                "stale_sources": sorted(self._stale),
                "debounce_ms": self.settings.watch_debounce_ms,
                "fallback_poll_seconds": self.settings.watch_poll_seconds,
                "writer_lock": str(self.settings.sync_lock_path),
                "watcher_lock": f"{self.settings.sync_lock_path}.watch",
            }

    def _publish_state(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_state_write < 1.0:
            return
        snapshot = self.snapshot()
        if not snapshot.get("leader"):
            return
        path = watcher_state_path(self.settings)
        payload = {
            **snapshot,
            "pid": os.getpid(),
            "heartbeat_at": _utc_now(),
            "updated_at_epoch": now,
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning("Could not publish Retrieval watcher heartbeat: %s", exc)
            self._last_state_write = now
            return
        self._last_state_write = now

    def _remove_published_state(self) -> None:
        path = watcher_state_path(self.settings)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if int(payload.get("pid", -1)) == os.getpid():
                path.unlink(missing_ok=True)
        except (OSError, TypeError, ValueError):
            return

    def _queue(self, names: Iterable[str], *, event: bool) -> None:
        names = set(names)
        if not names:
            return
        with self._mutex:
            self._pending.update(names)
            self._stale.update(names)
            self._last_event_monotonic = time.monotonic()
            if event:
                self._state["last_event_at"] = _utc_now()

    def _reconcile(self) -> None:
        with self._mutex:
            names = sorted(self._pending)
            self._state["sync_in_progress"] = True
            self._state["last_reconcile_at"] = _utc_now()
        try:
            result = self.service.sync(
                names=names,
                reason="watcher",
                only_if_stale=True,
                lock_timeout=0,
            )
            lock = result.get("lock") or {}
            if not lock.get("acquired"):
                with self._mutex:
                    self._state["lock_contentions"] += 1
                    self._retry_at = time.monotonic() + 2.0
                return
            successful = {
                str(report["source"])
                for report in result.get("synced", [])
                if not report.get("error")
                and not report.get("changed_during_sync")
            }
            skipped = {
                str(report["source"])
                for report in result.get("skipped", [])
            }
            unavailable = set(result.get("unavailable") or [])
            failed = {
                str(report["source"])
                for report in result.get("synced", [])
                if report.get("error") or report.get("changed_during_sync")
            }
            with self._mutex:
                completed = successful | skipped | unavailable
                self._pending.difference_update(completed)
                self._stale.difference_update(successful | skipped)
                self._pending.update(failed)
                self._stale.update(failed | unavailable)
                if successful:
                    self._state["last_sync_at"] = _utc_now()
                    self._state["last_sync_sources"] = sorted(successful)
                errors = [
                    str(report.get("error"))
                    for report in result.get("synced", [])
                    if report.get("error")
                ]
                self._state["last_error"] = "; ".join(errors)[:4000]
                self._retry_at = (
                    time.monotonic() + 30.0 if failed else 0.0
                )
        except Exception as exc:
            logger.exception("Retrieval watcher reconciliation failed")
            with self._mutex:
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
                self._retry_at = time.monotonic() + 30.0
        finally:
            with self._mutex:
                self._state["sync_in_progress"] = False

    def _run_leader(self) -> None:
        sources = [
            source
            for source in self.service.sources
            if source.enabled
        ]
        monitor: _InotifyMonitor | _PollingMonitor
        try:
            monitor = _InotifyMonitor(sources)
        except Exception as exc:
            logger.warning(
                "Native Retrieval watcher unavailable; using fingerprint polling: %s",
                exc,
            )
            monitor = _PollingMonitor(
                sources,
                self.settings.watch_poll_seconds,
            )
        safety_poll = (
            _PollingMonitor(sources, self.settings.watch_poll_seconds)
            if isinstance(monitor, _InotifyMonitor)
            else None
        )
        with self._mutex:
            self._state["backend"] = (
                f"{monitor.name}+fingerprint"
                if safety_poll is not None
                else monitor.name
            )
            self._state["healthy"] = True
            self._state["leader"] = True
        self._publish_state(force=True)
        self._queue((source.name for source in sources), event=False)
        try:
            while not self._stop.is_set():
                try:
                    changed, overflow = monitor.read(timeout=0.5)
                except Exception as exc:
                    if isinstance(monitor, _PollingMonitor):
                        raise
                    logger.warning(
                        "Retrieval inotify failed; switching to fingerprint polling: %s",
                        exc,
                    )
                    monitor.close()
                    monitor = safety_poll or _PollingMonitor(
                        sources, self.settings.watch_poll_seconds
                    )
                    safety_poll = None
                    with self._mutex:
                        self._state["backend"] = monitor.name
                        self._state["last_error"] = (
                            f"inotify: {type(exc).__name__}: {exc}"
                        )
                    changed = set()
                    overflow = True
                if safety_poll is not None:
                    polled, _ = safety_poll.read(timeout=0)
                    changed.update(polled)
                if overflow:
                    changed.update(source.name for source in sources)
                if changed:
                    for source_name in changed:
                        monitor.refresh_source(source_name)
                        if safety_poll is not None:
                            safety_poll.refresh_source(source_name)
                    self._queue(changed, event=True)
                with self._mutex:
                    pending = bool(self._pending)
                    quiet_for = (
                        time.monotonic() - self._last_event_monotonic
                    )
                    retry_ready = time.monotonic() >= self._retry_at
                if (
                    pending
                    and retry_ready
                    and quiet_for
                    >= self.settings.watch_debounce_ms / 1000
                ):
                    self._reconcile()
                self._publish_state()
        except Exception as exc:
            logger.exception("Retrieval source watcher stopped")
            with self._mutex:
                self._state["healthy"] = False
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            monitor.close()
            if safety_poll is not None:
                safety_poll.close()
            with self._mutex:
                self._state["healthy"] = False
                self._state["leader"] = False
            self._remove_published_state()

    def _run(self) -> None:
        watcher_lock_path = Path(f"{self.settings.sync_lock_path}.watch")
        watcher_lock_path.parent.mkdir(parents=True, exist_ok=True)
        leader_lock = FileLock(str(watcher_lock_path))
        while not self._stop.is_set():
            try:
                leader_lock.acquire(timeout=0)
            except FileLockTimeout:
                with self._mutex:
                    self._state["backend"] = "standby"
                    self._state["healthy"] = True
                    self._state["leader"] = False
                self._stop.wait(2.0)
                continue
            try:
                self._run_leader()
            finally:
                leader_lock.release()
            if not self._stop.is_set():
                self._stop.wait(2.0)
        with self._mutex:
            self._state["healthy"] = False
            self._state["leader"] = False
