from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tomllib

from .models import SourceConfig


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _diogenes_endpoint() -> dict[str, str]:
    explicit = os.getenv("DIOGENES_EMBEDDING_ENDPOINT_FILE", "").strip()
    root = os.getenv("DIOGENES_ROOT", "").strip()
    if explicit:
        endpoint_file = Path(os.path.expandvars(os.path.expanduser(explicit)))
    elif root:
        endpoint_file = Path(os.path.expandvars(os.path.expanduser(root))) / "data" / "embedding_endpoint.json"
    else:
        return {}
    if not endpoint_file.is_file():
        return {}
    try:
        payload = json.loads(endpoint_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "url": str(payload.get("url") or "").strip(),
        "model": str(payload.get("model") or "").strip(),
        # Diogenes encrypts this field with its installation-specific secret
        # store. Retrieval deliberately does not duplicate or bypass that
        # boundary; users can provide EMBEDDING_API_KEY directly when needed.
        "api_key": "",
    }


@dataclass(frozen=True)
class Settings:
    root: Path
    sources_file: Path
    chroma_host: str
    chroma_port: int
    chroma_ssl: bool
    archive_db: Path
    embedding_url: str
    embedding_model: str
    embedding_api_key: str
    embedding_batch_size: int
    embedding_max_chars: int
    fastembed_model: str
    fastembed_cache: Path
    hermes_command: str
    hermes_session_newer_than: str
    watch_enabled: bool
    watch_debounce_ms: int
    watch_poll_seconds: float
    sync_lock_path: Path
    sync_lock_timeout: float
    max_skills_per_load: int
    max_skill_chars: int
    max_total_skill_chars: int
    max_recall_chars: int
    taxonomy_file: Path
    category_overrides_file: Path
    skill_intake_root: Path
    catalog_root: Path
    projection_root: Path
    target_harness: str
    iwe_command: str
    iwe_source: Path
    omp_command: str
    scout_enabled: bool
    scout_timeout: int
    scout_max_calls: int
    scout_profile: str
    scout_home: Path
    scout_model: str
    projection_max_files: int
    projection_max_bytes: int
    hermes_config: Path
    omp_config: Path
    omp_mcp_config: Path

    def __post_init__(self) -> None:
        if self.target_harness not in {"hermes", "omp"}:
            raise ValueError(
                "RETRIEVAL_HARNESS must be either 'hermes' or 'omp'"
            )

    def projection_lane_root(self, harness: str) -> Path:
        lane = harness.strip().casefold()
        if lane not in {"hermes", "omp"}:
            raise ValueError(f"unsupported retrieval harness: {harness}")
        return self.projection_root / lane / "skills"

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        project_root = (root or Path(__file__).resolve().parents[2]).resolve()
        _load_dotenv(project_root / ".env")
        diogenes = _diogenes_endpoint()
        archive_db = Path(
            os.path.expandvars(
                os.path.expanduser(
                    os.getenv(
                        "RETRIEVAL_ARCHIVE_DB",
                        "~/.local/share/retrieval/archive.sqlite3",
                    )
                )
            )
        ).resolve()
        sync_lock_path = Path(
            os.path.expandvars(
                os.path.expanduser(
                    os.getenv(
                        "RETRIEVAL_SYNC_LOCK",
                        f"{archive_db}.sync.lock",
                    )
                )
            )
        ).resolve()
        return cls(
            root=project_root,
            sources_file=project_root / "sources.toml",
            chroma_host=os.getenv("RETRIEVAL_CHROMA_HOST", "127.0.0.1"),
            chroma_port=_int_env("RETRIEVAL_CHROMA_PORT", 8100),
            chroma_ssl=_bool_env("RETRIEVAL_CHROMA_SSL"),
            archive_db=archive_db,
            embedding_url=os.getenv("EMBEDDING_URL", "").strip() or diogenes.get("url", ""),
            embedding_model=os.getenv("EMBEDDING_MODEL", "").strip() or diogenes.get("model", ""),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", "").strip() or diogenes.get("api_key", ""),
            embedding_batch_size=max(1, _int_env("EMBEDDING_BATCH_SIZE", 8)),
            embedding_max_chars=max(400, _int_env("EMBEDDING_MAX_CHARS", 4000)),
            fastembed_model=os.getenv(
                "RETRIEVAL_FASTEMBED_MODEL",
                "sentence-transformers/all-MiniLM-L6-v2",
            ),
            fastembed_cache=Path(
                os.path.expandvars(
                    os.path.expanduser(os.getenv("RETRIEVAL_FASTEMBED_CACHE", "~/.cache/fastembed"))
                )
            ).resolve(),
            hermes_command=os.getenv("HERMES_COMMAND", "hermes"),
            hermes_session_newer_than=os.getenv("HERMES_SESSION_NEWER_THAN", "3650d"),
            watch_enabled=_bool_env("RETRIEVAL_WATCH_ENABLED", True),
            watch_debounce_ms=max(
                250,
                min(30_000, _int_env("RETRIEVAL_WATCH_DEBOUNCE_MS", 1500)),
            ),
            watch_poll_seconds=max(
                2.0,
                _float_env("RETRIEVAL_WATCH_POLL_SECONDS", 15.0),
            ),
            sync_lock_path=sync_lock_path,
            sync_lock_timeout=max(
                0.0,
                _float_env("RETRIEVAL_SYNC_LOCK_TIMEOUT", 30.0),
            ),
            max_skills_per_load=max(1, _int_env("RETRIEVAL_MAX_SKILLS_PER_LOAD", 6)),
            max_skill_chars=max(1000, _int_env("RETRIEVAL_MAX_SKILL_CHARS", 60000)),
            max_total_skill_chars=max(2000, _int_env("RETRIEVAL_MAX_TOTAL_SKILL_CHARS", 120000)),
            max_recall_chars=max(1000, _int_env("RETRIEVAL_MAX_RECALL_CHARS", 8000)),
            taxonomy_file=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv(
                            "RETRIEVAL_TAXONOMY_FILE",
                            "",
                        ).strip()
                        or str(project_root / "taxonomy.toml")
                    )
                )
            ).resolve(),
            category_overrides_file=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv(
                            "RETRIEVAL_CATEGORY_OVERRIDES",
                            "",
                        ).strip()
                        or str(project_root / "category-overrides.toml")
                    )
                )
            ).resolve(),
            skill_intake_root=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv(
                            "RETRIEVAL_SKILL_INTAKE",
                            "~/Hermes/skill-library",
                        )
                    )
                )
            ).absolute(),
            catalog_root=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv(
                            "RETRIEVAL_CATALOG_ROOT",
                            "~/.local/share/retrieval/catalog",
                        )
                    )
                )
            ).resolve(),
            projection_root=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv(
                            "RETRIEVAL_PROJECTION_ROOT",
                            "~/.local/share/retrieval/projections",
                        )
                    )
                )
            ).resolve(),
            target_harness=os.getenv("RETRIEVAL_HARNESS", "hermes")
            .strip()
            .casefold(),
            iwe_command=os.path.expandvars(
                os.path.expanduser(os.getenv("RETRIEVAL_IWE_COMMAND", "~/.cargo/bin/iwe"))
            ),
            iwe_source=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv("RETRIEVAL_IWE_SOURCE", "~/Hermes/iwe")
                    )
                )
            ).resolve(),
            omp_command=os.path.expandvars(
                os.path.expanduser(os.getenv("RETRIEVAL_OMP_COMMAND", "~/.bun/bin/omp"))
            ),
            scout_enabled=_bool_env("RETRIEVAL_SCOUT_ENABLED", True),
            scout_timeout=max(15, _int_env("RETRIEVAL_SCOUT_TIMEOUT", 120)),
            scout_max_calls=max(1, min(20, _int_env("RETRIEVAL_SCOUT_MAX_CALLS", 8))),
            scout_profile=os.getenv(
                "RETRIEVAL_SCOUT_PROFILE", "retrieval-scout"
            ).strip(),
            scout_home=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv(
                            "RETRIEVAL_SCOUT_HOME",
                            "~/.local/share/retrieval/scout-home",
                        )
                    )
                )
            ).resolve(),
            scout_model=os.getenv("RETRIEVAL_SCOUT_MODEL", "").strip(),
            projection_max_files=max(
                1, _int_env("RETRIEVAL_PROJECTION_MAX_FILES", 2000)
            ),
            projection_max_bytes=max(
                1024,
                _int_env("RETRIEVAL_PROJECTION_MAX_BYTES", 250 * 1024 * 1024),
            ),
            hermes_config=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv("RETRIEVAL_HERMES_CONFIG", "~/.hermes/config.yaml")
                    )
                )
            ).resolve(),
            omp_config=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv("RETRIEVAL_OMP_CONFIG", "~/.omp/agent/config.yml")
                    )
                )
            ).resolve(),
            omp_mcp_config=Path(
                os.path.expandvars(
                    os.path.expanduser(
                        os.getenv(
                            "RETRIEVAL_OMP_MCP_CONFIG",
                            "~/.omp/agent/mcp.json",
                        )
                    )
                )
            ).resolve(),
        )

    def sources(self) -> list[SourceConfig]:
        if not self.sources_file.exists():
            raise FileNotFoundError(
                f"{self.sources_file} is missing; copy sources.example.toml and review it"
            )
        payload = tomllib.loads(self.sources_file.read_text(encoding="utf-8"))
        out: list[SourceConfig] = []
        seen: set[str] = set()
        for row in payload.get("sources", []):
            name = str(row["name"]).strip()
            kind = str(row["kind"]).strip()
            if not name or name in seen:
                raise ValueError(f"source names must be non-empty and unique: {name!r}")
            # Older local catalogs included Librarian's OKF data. Keep those
            # installations upgrade-safe while enforcing the ownership
            # boundary: Librarian, not Retrieval, indexes and mutates it.
            if kind == "librarian":
                seen.add(name)
                continue
            if kind not in {
                "skills",
                "workflows",
                "context_mode",
                "hermes_sessions",
                "references",
            }:
                raise ValueError(f"unsupported source kind {kind!r} for {name}")
            raw_path = os.path.expandvars(os.path.expanduser(str(row["path"])))
            absolute_path = Path(raw_path).absolute()
            configured_state = str(row.get("state") or "").strip().lower()
            if configured_state:
                state = configured_state
            elif kind == "skills" and ".hermes/skills" in absolute_path.as_posix():
                state = (
                    "archived"
                    if "/.archive" in absolute_path.as_posix()
                    else "native"
                )
            else:
                state = "cold"
            if state not in {"native", "hidden", "cold", "archived"}:
                raise ValueError(
                    f"unsupported source state {state!r} for {name}; "
                    "use native, hidden, cold, or archived"
                )
            harness = str(row.get("harness") or "").strip().casefold()
            if not harness and kind == "skills":
                logical = absolute_path.as_posix()
                if "/.hermes/" in logical or logical.endswith("/.hermes"):
                    harness = "hermes"
                elif "/.omp/" in logical or logical.endswith("/.omp"):
                    harness = "omp"
            if harness not in {"", "hermes", "omp"}:
                raise ValueError(
                    f"unsupported source harness {harness!r} for {name}; "
                    "use hermes or omp"
                )
            out.append(
                SourceConfig(
                    name=name,
                    kind=kind,
                    # Preserve the configured logical path. Resolving here
                    # would erase evidence that a source root is a symlink,
                    # preventing the exact-ID admin CLI from rejecting a
                    # mutating operation through it.
                    path=absolute_path,
                    enabled=bool(row.get("enabled", True)),
                    state=state,
                    harness=harness,
                )
            )
            seen.add(name)
        return out
