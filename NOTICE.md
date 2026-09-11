# Third-party notices

`src/hermes_retrieval/graph_index.py` ports the small read-only ranking surface
that Retrieval formerly consumed from IWE: title/key fuzzy matching, BM25 with
`k1 = 1.2` and `b = 0.75`, and reciprocal-rank fusion with `k = 60`.

The design was reviewed against IWE commit
`6877ec10fe23a2150d87e44309c8e090f6528ee8`, primarily:

- `crates/diwe/src/search.rs`
- `crates/diwe/src/search_query.rs`
- `crates/iwes/src/router/server/search.rs`

IWE is copyright 2025 Dmytro Halichenko and licensed under Apache-2.0. A copy
of that license is included at `THIRD_PARTY_LICENSES/IWE-APACHE-2.0.txt`.

Retrieval does not install, invoke, vendor, or retain the IWE executable or
checkout. Its Python implementation and skill-specific graph model are part of
this AGPL-3.0-only project.
