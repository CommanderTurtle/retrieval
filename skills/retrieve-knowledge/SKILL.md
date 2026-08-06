---
name: retrieve-knowledge
description: Discover a highly specific dormant skill without exposing a large descriptor list to every prompt. Use when unfamiliar specialist guidance may exist in the external Retrieval catalog.
---

# Retrieve Knowledge

When a task may benefit from a specialist skill that is not already advertised,
call `retrieve_skill` once with the user's concrete intent. Retrieval delegates
selection to an ephemeral OMP process that can only search and read its IWE
catalog. It returns at most one canonical `SKILL.md` verbatim, so apply those
instructions immediately.

Retrieval knows whether this MCP process belongs to Hermes or OMP and can write
only to that harness's dedicated projection lane. It copies the complete
selected package there unless the same hidden skill is already installed in its
owning harness. The exact `SKILL.md` in the tool result is authoritative for the
current turn. An out-of-process stdio MCP cannot invoke an interactive host
slash command, so native discovery for later turns is refreshed explicitly:

- Hermes: run `/reload-skills` when future turns must discover the projection.
- OMP: run `/reload` for the same reason.

Do not attempt to maintain projections through MCP. A human can inspect both
lanes with `retrieval projected list` and remove selected copies through the
interactive `retrieval projected clear` checklist or explicit exact IDs. Those
CLI operations are manifest-guarded and cannot remove canonical or native
skills.

Do not use Retrieval for source-code graphs, ordinary web research, or curated
wiki synthesis. Use codebase-memory, Firecrawl/Camofox, and Librarian for those
respective jobs. Do not retrieve a skill that is already active.

When the task needs optional architecture, history, or a long runbook that was
deliberately extracted from native context, call `retrieve_reference`. Returned
sections are supporting material, not active rules. Never use it as a substitute
for the current repository's `AGENTS.md` or `RULES.md`.
