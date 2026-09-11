# Retrieval

Retrieval keeps large skill libraries searchable without advertising every
skill in every model prompt. Hermes and OMP retain their upstream skill sets
plus a small first-party routing set. Retrieval installs `retrieve-knowledge`;
the Localflame and Librarian installers own their corresponding routers. A
request to `retrieve-knowledge` can find, select, and return one complete
dormant `SKILL.md` in the same turn.

The repository owns the complete path:

- a local skill catalog with content-addressed duplicate merging;
- in-process BM25, fuzzy matching, reciprocal-rank fusion, and graph traversal;
- optional Chroma/FastEmbed semantic ranking;
- a read-only OMP selector with no ordinary tools or inherited MCP servers;
- manifest-owned temporary package projections for Hermes and OMP;
- session-close indexing, consolidation, and projection cleanup;
- a structured catalog tree for Diogenes;
- MCP tools and a small administrative CLI.

There is no IWE installation, checkout, process, binary, or configuration
dependency. The ranking behavior ported from its Apache-2.0 read-only search
surface is documented in [NOTICE.md](NOTICE.md).

## What a lookup does

1. `retrieve_skill` receives the concrete task intent.
2. Retrieval searches title, description, path, categories, tags, and a bounded
   descriptor using semantic and integrated lexical rankings.
3. An isolated OMP session may inspect only candidate metadata and bounded
   catalog context. It cannot use shell, filesystem, web, skills, rules, or MCP.
4. Retrieval validates the selected ID and reads the canonical package.
5. It returns the complete `SKILL.md` and copies the complete package to the
   requesting harness's temporary projection lane.
6. The calling model applies the instructions immediately in that turn.

The model does not need to browse a skill list first. The native routing skill
is deliberately direct: call once, receive one skill, continue working.

## Clean harness baselines

Retrieval treats harness skill directories as execution surfaces, not storage.
At session shutdown it:

1. finds skill packages in the current harness and configured external roots;
2. hashes every file in each package;
3. copies non-stock or modified packages to
   `~/Hermes/skill-library/<name>-<hash>`;
4. merges byte-identical packages and records every origin;
5. indexes the durable copies;
6. restores modified Hermes bundled packages from the installed upstream tree;
7. removes the now-durable non-stock harness copies;
8. clears only projection directories listed in Retrieval's ownership manifest;
9. refreshes the catalog after cleanup.

Skills with the same name but different bytes remain distinct. Packages that
contain symlinks are indexed and reported but not removed automatically. The
source of a configured external directory is never deleted.

Hermes uses its native `on_session_end` shell-hook contract. OMP uses its native
`session_before_switch` and `session_shutdown` extension events, covering
`/new`, `/resume`, `/fork`, and process exit. Both dispatch the same
lock-protected local worker and return immediately, so closing or replacing a
session does not stall the UI.

After setup, the only non-upstream skills retained by session-close are the
exact managed routers `retrieve-knowledge`, `localflame`, and `librarian` when
their owning integrations are installed. The empty projection lane remains
configured so a chosen package can become visible after `/reload-skills` in
Hermes or `/reload` in OMP. No generic frontmatter or directory exemption is
used; only those exact harness-relative paths bypass consolidation.

## Install and update

Requirements:

- Linux or WSL;
- Python 3.11-3.13;
- `uv`;
- Hermes and/or OMP when those integrations are desired;
- Chroma at the configured address for semantic ranking. Lexical retrieval does
  not depend on it.

```bash
git clone https://github.com/CommanderTurtle/retrieval.git ~/Hermes/retrieval
cd ~/Hermes/retrieval
./setup.sh
```

`setup.sh` creates the uv environment, consolidates current installed skills,
restores clean harness baselines, builds the catalog, and installs the router,
MCP entries, and lifecycle integrations. Review `.env` and `sources.toml` to
change explicit source locations.

Update with:

```bash
~/Hermes/retrieval/update.sh
```

Reapply only the installed harness contract, or audit it without changing
anything:

```bash
~/Hermes/retrieval/integrate.sh
~/Hermes/retrieval/doctor.sh
```

The doctor reads every ordinary and isolated Hermes/OMP profile and verifies
the Retrieval MCP, projection lane, routing skill, and session-close lifecycle
boundary. For Hermes it also checks the native MCP, tool, and hook registries,
plus the exact hook consent and current worker mtime. It does not start an MCP
server, call a model, or access the network.

The update is fast-forward-only through Git, syncs the locked uv environment,
reconciles every existing profile, and reapplies integrations. It is safe to run
repeatedly.

Catalog schema 5 uses `~/.local/share/retrieval/catalog-v5`. An unchanged
legacy `RETRIEVAL_CATALOG_ROOT=~/.local/share/retrieval/catalog` value is mapped
there at load time, which keeps an older MCP process from rewriting the current
catalog during a rolling harness update.

## Native harness integration

Retrieval uses harness-owned configuration surfaces wherever they exist:

- Hermes MCP and skill-directory settings are written through
  `hermes config set`.
- The Hermes lifecycle entry is written through `hermes config set` and is
  visible through `hermes hooks list`. Hermes has no targeted hook-approval
  command, so Retrieval calls Hermes's installed consent implementation for
  only its exact session-close command. It never enables blanket hook consent;
  isolated profiles revoke an obsolete owned approval through
  `hermes hooks revoke`.
- OMP skill-directory settings are written through `omp config set`.
- OMP MCP configuration uses its documented `~/.omp/agent/mcp.json` surface;
  OMP has an interactive `/mcp` command but no equivalent non-interactive CLI
  registration command.
- The OMP shutdown extension is installed in its normal `extensions` directory.

The installer covers the default home and profiles that already use the normal
external-tool stack. Retrieval's own `retrieval-scout` OMP profile, empty audit
profiles, and Librarian's private `librarian-okf` worker are excluded from MCP,
router-skill, and shutdown integration. Re-running the installer also removes
unchanged Retrieval-owned artifacts from a profile that later becomes isolated.

## Source policy

Sources are explicit `[[sources]]` entries in `sources.toml`. Retrieval does not
scan the home directory. Common states are:

- `native`: advertised by one harness and available to the other;
- `hidden`: installed but omitted from that harness's model discovery;
- `cold`: stored only in Retrieval;
- `archived`: searchable but dormant.

`~/Hermes/skill-library` is the durable intake populated by session-close
consolidation. Repository-owned skill trees under `~/Hermes` are cold sources;
they are not kept in either harness's active custom-directory list.
Localflame's repository-level `SKILL.md` is likewise indexed cold from
`~/Deepseek/localflame`, so installing its MCP never adds a second native skill
to Hermes or OMP.

Category assignments come from `taxonomy.toml`, reviewed frontmatter, or exact
IDs in `category-overrides.toml`. Unclassified packages remain in the audit
queue and are not projected until reviewed.

## Search and graph

The integrated graph indexes one compact descriptor per skill rather than its
whole body. Its lexical surface follows the former IWE behavior closely:

- BM25 with `k1=1.2` and `b=0.75`;
- fuzzy matching over stable identity and title;
- reciprocal-rank fusion with `k=60`;
- deterministic ID tie-breaking.

Source, category, tag, and state relationships provide bounded neighbor
context. The structured form used by the Diogenes explorer is available
without scraping generated Markdown:

```bash
retrieval catalog browse
```

The response includes facets, graph nodes and edges, source/category roots,
review records, and compact skill metadata. Exact whole-package hashes drive
duplicate merging. Provenance fields retain all duplicate paths, source names,
and item IDs; equal display names with different package contents remain
separately searchable.

## CLI

```bash
retrieval status
retrieval sync
retrieval search "windows service debugging"
retrieval retrieve "audit a browser extension" --harness hermes

retrieval catalog sync
retrieval catalog stats
retrieval catalog browse
retrieval catalog audit
retrieval catalog audit /path/to/intake --name candidate

retrieval projected list --harness all
retrieval projected clear --harness hermes --all

retrieval session-close --harness hermes
retrieval session-close --harness omp
retrieval session-close --harness hermes --all-profiles

retrieval references "architecture rationale"
retrieval context audit /path/to/repository
retrieval integrate
```

`session-close` is also useful as a deterministic manual repair. It is
idempotent: a content-addressed package already in intake is not copied again.

## MCP surface

The public server exposes only `retrieve_skill` and `retrieve_reference`.
Search and bounded graph reads are private capabilities of the isolated
selector; catalog browsing, workflow compatibility commands, projection
inspection, and cleanup remain CLI operations. MCP tools cannot delete a
canonical package or clear another harness's projection lane.

## Diogenes

Diogenes consumes `retrieval catalog browse` and displays the source/category
tree, skill records, duplicate provenance, and review queue. It no longer
parses the filesystem into a flat skill list and does not require a separate
graph service.

The Retrieval runtime depends only on its Chroma service for the optional
semantic lane. Lexical/fuzzy/graph search stays available in-process.

## Data and safety

- Source packages and harness session stores remain authoritative.
- Catalog cards, Chroma collections, and projections are disposable indexes.
- Projection cleanup validates both an ownership manifest and per-directory
  markers.
- Intake removal happens only after a copied package produces the same complete
  bundle hash.
- Modified stock packages are consolidated before replacement.
- Paths with symlinks are never automatically removed.
- Concurrent shutdowns serialize through a host-local file lock.
- Chroma anonymized telemetry is disabled in the client.

Logs from background session-close workers are written to
`~/.local/state/retrieval/session-close.log` unless `XDG_STATE_HOME` changes the
state root.

## License

Retrieval is licensed under AGPL-3.0-only. See [NOTICE.md](NOTICE.md) and
`THIRD_PARTY_LICENSES/` for the ported ranking surface's Apache-2.0 notice.
