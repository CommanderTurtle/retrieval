# Hermes Retrieval

Hermes Retrieval keeps large, specialist skill libraries out of an agent's
always-on skill inventory without making them hard to use. It builds one compact
descriptor per skill, combines semantic ranking with an
[IWE](https://iwe.md/) Markdown graph, and delegates the final choice to a
short-lived, read-only OMP scout.

The selected `SKILL.md` is returned verbatim in the same MCP response, so the
calling agent can use it immediately. Retrieval also copies the skill's complete
package into one manifest-owned projection directory shared by Hermes and OMP.
That temporary copy survives context compaction and becomes natively discoverable
after `/reload-skills` in Hermes or `/reload` in OMP. Clearing it never touches the
canonical repository.

## What Retrieval owns

Retrieval has one intentionally narrow responsibility: cold-skill discovery.

- `native` skills already live in a harness-visible tree; their names suppress
  cold duplicates, but they are neither graphed nor embedded as candidates;
- `cold` skills remain only in external repositories until selected;
- `archived` skills remain discoverable but dormant;
- the generated IWE catalog and Chroma collections are disposable indexes;
- the projection manifest owns every temporary skill copy it is allowed to
  remove.

Context Mode owns its context database. Hermes and OMP own their sessions,
compaction, active skill discovery, and tool history. Librarian owns delegated
synthesis and editable knowledge. `codebase-memory-mcp` owns repository graphs.
Those sources can still be indexed through the administrative compatibility
CLI, but they are disabled in the recommended configuration and are not exposed
as Retrieval MCP tools.

## Selection flow

1. Chroma ranks one short descriptor per cold or archived skill semantically.
2. IWE performs fuzzy title and BM25 search over the same skills and exposes
   controlled source/category graph links.
3. Reciprocal-rank fusion produces a bounded candidate list.
4. An ephemeral OMP RPC process searches and reads candidates using exactly two
   host-owned, read-only tools. Native tools, MCPs, skills, rules, extensions,
   sessions, LSP, PTY, and the advisor are disabled in its isolated profile.
   Its HOME and XDG roots are also private, preventing OMP's cross-harness
   capability discovery from importing Zed, Claude, or other user MCP configs.
5. The scout may select at most one skill. It must first discover and inspect the
   selected ID; otherwise Retrieval fails closed.
6. Retrieval returns the canonical `SKILL.md` verbatim and atomically projects
   the full package. Symlinks are skipped and file/byte limits are enforced.

The IWE tree is derived from configured sources under
`~/.local/share/hermes-retrieval/catalog`. It contains readable skill cards plus
a deliberately small category taxonomy; it is not another canonical library.

## Requirements

- [uv](https://docs.astral.sh/uv/) and Python 3.11 through 3.13;
- a reachable Chroma server;
- either an OpenAI-compatible embedding endpoint or the local FastEmbed fallback;
- [IWE](https://github.com/iwe-org/iwe) (`setup.sh` installs its CLI with Cargo
  when absent);
- an existing [oh-my-pi](https://github.com/can1357/oh-my-pi) installation for
  delegated selection.

Retrieval does not install Node, npm, or pnpm. OMP may be installed and updated
through the user's existing Bun lifecycle.

## Setup

```bash
git clone https://github.com/CommanderTurtle/retrieval.git
cd retrieval
./setup.sh
```

`setup.sh` creates `.env`, `sources.toml`, and `category-overrides.toml` only
when missing, creates the persistent `~/Hermes/skill-library` intake root, builds
the uv-managed `.venv`, installs IWE when needed, builds the catalog, and runs
the idempotent harness integration. Review the local files before starting:

```bash
${EDITOR:-vi} .env
${EDITOR:-vi} sources.toml
./install-watcher.sh
./start.sh
```

Every skill source has an explicit state:

```toml
[[sources]]
name = "hermes-skills"
kind = "skills"
path = "~/.hermes/skills"
enabled = true
state = "native"

[[sources]]
name = "specialist-library"
kind = "skills"
path = "~/Hermes/specialist-library"
enabled = true
state = "cold"
```

No home-directory discovery scan occurs. `hermes-retrieval integrate` adds only
the shared projection root to Hermes `skills.external_dirs` and OMP
`skills.customDirectories`. It also prepares a Retrieval-owned OMP profile with
the configured model/provider but no inherited MCPs or agent extensions. The
command is idempotent and does not require a gateway restart.

## Maintaining specialist skill libraries

The easiest future intake is a clone beneath the one configured pointer root:

```bash
git clone https://github.com/example/security-skills.git \
  ~/Hermes/skill-library/security-skills
.venv/bin/hermes-retrieval catalog audit
```

The persistent watcher already follows that root recursively. A new or edited
`SKILL.md` is categorized, reduced to its compact descriptor, and synchronized
without polling on searches. No separate source entry or MCP restart is needed.

Categorization is intentionally conservative:

- `taxonomy.toml` is the committed, stable category vocabulary. Existing IDs do
  not change as new libraries arrive;
- a skill may explicitly declare `retrieval_categories` in its frontmatter;
- `category-overrides.toml` is the ignored, human-owned assignment layer for
  upstream repositories that should remain untouched;
- keyword matches may assign only categories already present in the taxonomy;
- a skill with no approved category enters the review queue and is excluded from
  both IWE and Chroma. Retrieval never invents a category automatically.

An exact override looks like this:

```toml
[skills]
"security-skills:skills/packet-hunter" = ["cybersecurity", "network-security"]
```

If no existing category is accurate, deliberately append a new
`[[categories]]` table to `taxonomy.toml`, add the exact override, then run:

```bash
.venv/bin/hermes-retrieval catalog audit
.venv/bin/hermes-retrieval sync skill-intake
```

To inspect a directory without moving or registering it:

```bash
.venv/bin/hermes-retrieval catalog audit /path/to/skills --name future-skills
```

To keep an approved tree at an arbitrary location, preview and then register it:

```bash
.venv/bin/hermes-retrieval catalog register future-skills /path/to/skills --dry-run
.venv/bin/hermes-retrieval catalog register future-skills /path/to/skills
```

Registration refuses any pending review, appends only one explicit local source,
synchronizes it, and restarts the watcher when that user service is active. These
maintenance operations are human-only CLI functions and are never exposed by the
Retrieval MCP.

## MCP surface

The server exposes only three tools:

- `retrieve_skill(query)` searches, inspects, returns, and projects at most one
  specialist skill;
- `list_retrieved_skills()` reports only Retrieval-owned temporary packages;
- `clear_retrieved_skills(skill_ids?)` removes selected projections, or all when
  IDs are omitted.

`start.sh` is a stdio MCP process. Register its absolute path with the Hermes MCP
CLI, then install the small always-on routing skill in the profiles that should
know when to call it:

```bash
./install-hermes-skill.sh
./install-hermes-skill.sh --profile librarian
```

## CLI

The normal human-facing path is one fused search command:

```bash
.venv/bin/hermes-retrieval search "audit Kubernetes pod security" --limit 8
```

Administrative commands are explicit:

```bash
.venv/bin/hermes-retrieval retrieve "audit Kubernetes pod security"
.venv/bin/hermes-retrieval projected list
.venv/bin/hermes-retrieval projected clear
.venv/bin/hermes-retrieval catalog sync
.venv/bin/hermes-retrieval catalog stats
.venv/bin/hermes-retrieval catalog audit
.venv/bin/hermes-retrieval integrate
.venv/bin/hermes-retrieval status
.venv/bin/hermes-retrieval sync
```

The watcher keeps descriptors and the IWE catalog current after source changes;
search calls do not rescan the filesystem. `sync` remains a recovery/admin
command. Exact-ID `skills list`, `inspect`, `edit`, `archive`, and `restore`
commands remain available for deliberate human housekeeping. Archive and restore
never operate on a fuzzy match.

## Safety and privacy

- Canonical skill repositories are read-only to retrieval and projection flows.
- Clear operations require both a manifest entry and a matching per-directory
  ownership marker.
- Projections are staged and atomically replaced.
- The scout rejects any OMP process that exposes tools beyond its two registered
  catalog tools.
- Catalog fields and skill excerpts are explicitly treated as untrusted data by
  the scout.
- Chroma anonymized telemetry, OMP telemetry, and update checks in the isolated
  profile are disabled.
- Network requests are limited to the configured Chroma/model endpoints and
  user-invoked package installation/update sources.
- `.env`, `sources.toml`, `category-overrides.toml`, local databases, generated
  catalogs, projections, caches, and the uv environment are excluded from Git.

## Update and development

```bash
./update.sh
uv sync --frozen
uv run --frozen pytest -q
uv build
```

`update.sh` remains fast-forward-only. After source updates, restart a persistent
watcher or Hermes gateway only when its already-running process must load new
Python code; skill inventory refresh itself uses `/reload-skills` or `/reload`.

## License

AGPLv3.0 - See [`LICENSE`](LICENSE).
