# Hermes Retrieval

A small semantic routing layer for Hermes. It indexes explicitly configured
skills, redacted Hermes exports, context-mode chunks, and Librarian knowledge
without making any of them part of every prompt.

Canonical files remain in their original source. Ephemeral context-mode and
Hermes session events are preserved in an ordered local SQLite archive before
Chroma indexes them, so an upstream cleanup cannot erase history. Chroma remains
a disposable index. Source-code structure belongs to `codebase-memory-mcp`;
curated knowledge and wiki edits belong to Librarian.

`EMBEDDING_URL` uses an OpenAI-compatible endpoint. If `DIOGENES_ROOT` is set,
the service also follows Diogenes' saved embedding endpoint and can share its
FastEmbed cache.

```bash
./setup.sh
./start.sh
```

Use `hermes-retrieval sync`, `hermes-retrieval status`, or the seven MCP tools:
`find_skills`, `load_skills`, `find_workflows`, `load_workflows`, `recall`,
`sync_sources`, and `retrieval_status`. Workflows are opt-in agent personas,
commands, and hook definitions. Loading a hook returns its canonical source but
never activates it.

Install the small native routing skill after registering the MCP:

```bash
./install-hermes-skill.sh
./install-hermes-skill.sh --profile librarian
```
