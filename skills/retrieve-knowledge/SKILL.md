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

For cold or archived skills, Retrieval also copies the complete selected
package into its dedicated shared projection directory. Hidden OMP skills are
already installed and are returned without a duplicate copy. The current
process does not automatically rescan projected skills:

- Hermes: run `/reload-skills` when future turns must discover the projection.
- OMP: run `/reload` for the same reason.

Use `list_retrieved_skills` to inspect those temporary packages and
`clear_retrieved_skills` when they are no longer relevant. Clear only through
that tool; it is manifest-guarded and cannot remove canonical or native skills.

Do not use Retrieval for source-code graphs, ordinary web research, or curated
wiki synthesis. Use codebase-memory, Firecrawl/Camofox, and Librarian for those
respective jobs. Do not retrieve a skill that is already active.

When the task needs optional architecture, history, or a long runbook that was
deliberately extracted from native context, call `retrieve_reference`. Returned
sections are supporting material, not active rules. Never use it as a substitute
for the current repository's `AGENTS.md` or `RULES.md`.
