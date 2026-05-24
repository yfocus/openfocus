# SPDX-License-Identifier: Apache-2.0
# Code Navigation Domain

## Scope

The code navigation domain powers AgentSpace editor navigation APIs:

- workspace search
- workspace/file symbols
- likely definitions
- text references
- backend capability status

This slice intentionally implements safe fallback behavior only. It does not
modify Companion protobuf contracts and does not start semantic language
servers.

## Security

All file access must go through the existing Companion AgentSpace file seam.
The domain must rely on Companion root validation for `AgentSpace.root_path`,
reject or drop unsafe relative paths in returned DTOs, and never return absolute
or traversal paths.

The fallback backend recursively lists and reads files through whitelisted
Companion file commands. It skips heavy default directories and binary-looking
content, enforces query/symbol/result limits, and returns stable frontend DTOs.
V1 fallback search rejects `regex=true` instead of executing user-provided
regular expressions in request handlers.

Search responses expose both `results` and `groups`. `results` remains the flat
ordered list for compatibility. `groups` is grouped by `path` in first-seen
result order, with each group shaped as `{ path, results }`, so editor overlays
can render file-grouped search results without re-grouping backend data.

## Backends

Current backends:

- `text_fallback` for search
- `symbol_fallback` for symbols
- `definition_fallback` for likely definitions
- `reference_fallback` for references

Future semantic/LSP support must degrade to these fallback backends when
unavailable or failing.
