---
name: retrieve-knowledge
description: Discover a highly specific dormant skill without exposing a large descriptor list to every prompt. Use when unfamiliar specialist guidance may exist in the external Retrieval catalog.
---

# Retrieve Knowledge

When a task may benefit from a specialist skill that is not already active,
call `retrieve_skill` once with the user's concrete intent. Retrieval delegates
selection to an ephemeral OMP process that can only search and read its IWE
catalog. It returns at most one canonical `SKILL.md` verbatim, so apply those
instructions immediately.

Retrieval also copies the complete selected package into its dedicated shared
projection directory. The current process does not automatically rescan skills:

- Hermes: run `/reload-skills` when future turns must discover the projection.
- OMP: run `/reload` for the same reason.

Use `list_retrieved_skills` to inspect those temporary packages and
`clear_retrieved_skills` when they are no longer relevant. Clear only through
that tool; it is manifest-guarded and cannot remove canonical or native skills.

Do not use Retrieval for source-code graphs, ordinary web research, or curated
wiki synthesis. Use codebase-memory, Firecrawl/Camofox, and Librarian for those
respective jobs. Do not retrieve a skill that is already active.
