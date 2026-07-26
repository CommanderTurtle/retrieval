# Hermes Retrieval

Hermes Retrieval is a small, source-aware semantic routing layer for Hermes. It
indexes explicitly configured skills, redacted Hermes exports, context-mode
chunks, and opt-in workflow definitions without placing those libraries in
every prompt.

Canonical files remain in their original source. Ephemeral context-mode and
Hermes session events are first preserved in an ordered local SQLite archive,
then indexed in Chroma, so an upstream cleanup cannot erase history. Chroma is
a disposable index. Source-code structure belongs to `codebase-memory-mcp`;
curated knowledge and wiki edits belong to Librarian.

That boundary is intentional. Retrieval is the thin durable history and
catalog layer: it preserves session identity, timestamps, insertion sequence,
tool-call/result adjacency, and context-mode linkage before indexing changed
records. It does not ingest or mutate Librarian's OKF knowledge and does not
build code graphs or wikis. Librarian owns delegated synthesis and editable
knowledge; `codebase-memory-mcp` owns repository structure.

## Requirements

- [uv](https://docs.astral.sh/uv/);
- Python 3.11 through 3.13 (setup defaults to uv-managed Python 3.13.12);
- a reachable Chroma server;
- either an OpenAI-compatible embedding endpoint or the local FastEmbed
  fallback.

The project has no JavaScript runtime requirement. It does not install Node,
npm, pnpm, or Bun.

## Setup

```bash
git clone https://github.com/CommanderTurtle/retrieval.git
cd retrieval
./setup.sh
```

`setup.sh` creates `.env` and `sources.toml` only when they are missing, creates
the local `.venv` with uv, and synchronizes the committed lock. Set
`RETRIEVAL_PYTHON` in the invoking shell to select another supported
interpreter.

Review both local configuration files before starting:

```bash
${EDITOR:-vi} .env
${EDITOR:-vi} sources.toml
./start.sh
```

`start.sh` is a stdio MCP process, not a network dashboard. Register its
absolute path through the Hermes MCP UI or CLI. Setup deliberately does not
change Hermes profiles, MCP registrations, hooks, or gateway state.

The MCP starts its own source watcher. On Linux and WSL it uses native inotify;
other platforms fall back to inexpensive source-fingerprint polling. Startup
reconciles missing or stale collections once. After that, filesystem and
SQLite/WAL events are debounced and only affected configured sources are
reconciled. A cross-process leader lock elects one watcher, and a separate
writer lock prevents the default and Librarian Hermes profiles from racing.
Search calls never scan sources or trigger maintenance.

One configured Hermes root includes its default session database, named
profiles beneath `profiles/`, and context-mode plugin metrics. Exports are
explicitly scoped per profile, so whichever MCP process wins watcher leadership
cannot omit the other profile's sessions.

## Sources and tools

The example catalog includes installed Hermes skills, generated Firecrawl
skills, Hermes Workspace skills and agent definitions, and explicitly cloned
libraries. No home-directory discovery scan occurs. Enabled or disabled state
is a Hermes concern; Retrieval indexes both so specialist guidance remains
discoverable without activating it.

The seven MCP tools are:

- `find_skills` and `load_skills`;
- `find_workflows` and `load_workflows`;
- `recall`;
- `sync_sources`;
- `retrieval_status`.

Workflows are opt-in agent personas, commands, and hook definitions. Loading a
hook returns its canonical source but never activates it.

The CLI exposes the same core operations:

```bash
.venv/bin/hermes-retrieval status
.venv/bin/hermes-retrieval sync
.venv/bin/hermes-retrieval recall "prior CUDA decision"
```

`sync` is an explicit recovery/admin command; routine source refresh is
automatic while the MCP is connected. `status` reports watcher leadership,
backend health, pending/stale sources, checkpoints, collection counts, and the
last successful refresh.

Skill files also have a small exact-ID housekeeping CLI:

```bash
.venv/bin/hermes-retrieval skills list
.venv/bin/hermes-retrieval skills inspect hermes-skills:research/example
.venv/bin/hermes-retrieval skills edit hermes-skills:research/example
.venv/bin/hermes-retrieval skills archive hermes-skills:research/example
.venv/bin/hermes-retrieval skills restore hermes-skills:research/example
```

The list is newest-first with stable names and exact IDs. Edit uses `VISUAL`
then `EDITOR`. Archive is never automatic: it only accepts an exact ID, rejects
symlinks and paths outside the configured source/archive roots, and moves one
skill into the private `RETRIEVAL_SKILL_ARCHIVE`. Restore reverses that move.
Archive/restore events are observed like any other source change, so their
vectors are removed or restored automatically.

## Embeddings and privacy

`EMBEDDING_URL` uses an explicitly configured OpenAI-compatible endpoint. If
`DIOGENES_ROOT` is set, Retrieval can follow Diogenes' saved embedding endpoint
and share its FastEmbed cache. It never copies or decrypts Diogenes' stored API
key; provide `EMBEDDING_API_KEY` locally when required.

Retrieval sends no telemetry. Its Chroma client explicitly disables anonymized
telemetry. Network requests go only to the Chroma and embedding endpoints named
in local configuration. `.env`, `sources.toml`, the uv environment, caches, and
the SQLite archive are excluded from Git.

When Diogenes publishes an embedding endpoint, Retrieval reuses that HTTP lane
and the existing Chroma service; it does not launch another model server.
FastEmbed remains the zero-configuration in-process fallback when no HTTP lane
is configured.

## Hermes routing skill

After registering the MCP, install the small always-on routing skill only in
the profiles that should use it:

```bash
./install-hermes-skill.sh
./install-hermes-skill.sh --profile librarian
```

The separate workflow installer supports `--dry-run`; it never activates
third-party hooks.

## Update and development

```bash
./update.sh
```

Updates are fast-forward-only and reconcile the existing local `.venv` from
`uv.lock`. Restart the Hermes gateway yourself when you are ready for it to
reload the MCP process.

```bash
uv sync --frozen
uv run --frozen pytest -q
uv build
```

## License

AGPLv3.0 - See [`LICENSE`](LICENSE).
