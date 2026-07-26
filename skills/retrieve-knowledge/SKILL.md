---
name: retrieve-knowledge
description: Route specialized instructions, opt-in workflows, source-code structure, prior session context, and editable project knowledge without preloading large libraries. Use when a task may need an unfamiliar skill, an Agent Skills persona or command, historical Hermes/context-mode evidence, codebase relationships, or a Librarian-maintained wiki.
---

# Retrieve Knowledge

Choose the narrowest source that can answer the task:

- Specialized guidance: call `find_skills` with the task intent. Select only
  applicable IDs, then call `load_skills`. Multiple IDs may be loaded together.
- Agent persona, reusable command, or hook definition: call `find_workflows`,
  optionally filtering by `agent`, `command`, or `hook`, then call
  `load_workflows` for selected IDs. Loading a hook never activates it.
- Source-code symbols, callers, architecture, changes, or paths: use
  `codebase-memory-mcp`. Index the repository only when it is absent or stale.
- Earlier decisions, commands, tool results, or a point in session history:
  call `recall`. Use `before` and `after` to reconstruct the ordered event
  sequence instead of treating an isolated match as the whole context.
- Curated, reusable, or editable project knowledge: query Librarian. Ask
  Librarian to research and write or update OKF knowledge when durable
  documentation is the desired result.

For broad research or wiki work, delegate to Librarian. A Librarian process has
retrieval and codebase-memory available, so describe the question, relevant
project, desired evidence, and whether it may edit the knowledge base.

Keep context bounded:

1. Discover before loading.
2. Load only what will change the work.
3. Treat returned locators as canonical; Chroma is only an index.
4. Prefer codebase-memory for program structure and retrieval for semantic
   history. Do not duplicate the source graph into Chroma.
5. Do not install hooks or bulk-enable skill libraries merely because their
   definitions were found.
6. Refresh sources only when status shows missing or stale data, not on every
   prompt.
