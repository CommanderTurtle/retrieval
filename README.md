# Hermes Retrieval

A small semantic routing layer for Hermes. It indexes explicitly configured
skills, redacted Hermes exports, context-mode chunks, and Librarian knowledge
without making any of them part of every prompt.

Canonical content always remains in its original source. Chroma is a disposable
index. Source-code structure belongs to `codebase-memory-mcp`; curated knowledge
and wiki edits belong to Librarian.

`EMBEDDING_URL` uses an OpenAI-compatible endpoint. If `DIOGENES_ROOT` is set,
the service also follows Diogenes' saved embedding endpoint and can share its
FastEmbed cache.

```bash
./setup.sh
./start.sh
```

Use `hermes-retrieval sync`, `hermes-retrieval status`, or the five MCP tools:
`find_skills`, `load_skills`, `recall`, `sync_sources`, and
`retrieval_status`.
