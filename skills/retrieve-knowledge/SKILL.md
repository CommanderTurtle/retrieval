---
name: retrieve-knowledge
description: Find and load a dormant specialist skill through Retrieval in the current turn. Use whenever relevant instructions may exist beyond the small native skill list.
---

# Retrieve Knowledge

When a task may benefit from instructions not already advertised, call
`retrieve_skill` once with the user's concrete intent. Retrieval merges exact
duplicates, searches its integrated lexical/semantic graph, and uses a
read-only selector to return at most one complete `SKILL.md`.

Read and apply the returned instructions immediately in the same turn. Do not
ask the user to run another command, search the filesystem yourself, enumerate
the library first, or narrate the lookup unless the tool reports an error.

Retrieval knows whether this MCP process belongs to Hermes or OMP. The exact
`SKILL.md` in the tool result is authoritative for the current turn. Hermes
may also receive a manifest-owned temporary package copy for native discovery;
use `/reload-skills` only when a later Hermes turn needs that copy. OMP is
strictly current-turn-only: no retrieved package is written into an OMP skill
tree and no reload is needed.

Hermes session shutdown indexes newly installed skill packages, consolidates
identical copies into Retrieval's durable library, and removes temporary
Hermes projections. Do not maintain or delete projection directories yourself.

Do not use Retrieval for source-code graphs, ordinary web research, or curated
wiki synthesis. Use codebase-memory, Localflame, and Librarian for those jobs.
Do not retrieve a skill that is already active.

When the task needs optional architecture, history, or a long runbook that was
deliberately extracted from native context, call `retrieve_reference`. Returned
sections are supporting material, not active rules. Never use them instead of
the current repository's `AGENTS.md` or `RULES.md`.
