<!-- SPDX-License-Identifier: Apache-2.0 -->
# Companion Domain SPEC

## Responsibility

The Companion domain owns OpenFocus-side companion device state and pairing rules:

- register and identify Companion devices by stable server-issued `companion_id`
- track online/offline/pending pairing state from the gRPC control connection
- validate pairing attempts and persist successful pairing credentials
- expose Companion capability state for AgentSpace, Inspiration terminal, runtime hook, and system inbox flows
- manage the selected System Inbox target Companion
- build a lightweight in-process AgentSpace file path list cache for editor
  Open File flows, populated through existing Companion file list requests

## Boundaries

- `openfocus.companion` owns the local Companion runtime process and gRPC client implementation.
- The Companion domain owns persisted server-side state and product rules around trust, status, capabilities, and pairing.
- Companion must not expose a browser-facing HTTP server; browser actions go through OpenFocus Web/API routes.
- Companion domain use cases expose domain-shaped errors, not FastAPI `HTTPException`; Web/API routes own HTTP status mapping and response adaptation.
- OpenFocus sends Companion gRPC/data-plane commands through the Companion domain command port seam, so use cases can be tested with fake ports without starting the real gRPC runtime.
- AgentSpace file helpers return domain payloads and domain errors: JSON-shaped list/read payloads for metadata and text, `CompanionRawFileResult` for raw bytes plus MIME type, and `CompanionFile*Error`/`CompanionRuntimeError` for file failures. Web/API routes adapt those payloads to HTTP responses, including raw `Response` construction.
- AgentSpace file path listing uses recursive OpenFocus-side traversal over the
  existing Companion `request_files_list` command. It must not require protobuf
  changes, persistent indexes, frontend state, or direct filesystem access from
  OpenFocus.
- AgentSpace online selection helpers expose domain errors for missing spaces, unbound spaces, unpaired Companions, and missing gRPC registry entries; Web/API routes map those errors to HTTP 404/400/502.
- Terminal lifecycle ownership belongs to `openfocus/domains/agent_spaces/`.
- Runtime activity projection belongs to `openfocus/domains/agent_activity/`.

## Invariants

- A Companion starts as `pending_certification` until the user completes pairing.
- Pairing codes are 10 letters or digits and are rate-limited to 10 attempts per minute per Companion.
- Only paired and active Companions may be selected as System Inbox targets.
- Capability checks are required before issuing system float ball or terminal commands.
- Disconnects must promptly mark the device offline without deleting historical state.
- AgentSpace file path cache entries are non-persistent and keyed by AgentSpace
  id, Companion id, and AgentSpace root path. A `refresh` request bypasses the
  TTL and replaces the cache entry only after a successful scan.
- File path list responses must contain only sanitized relative file paths:
  absolute paths, Windows drive paths, traversal segments, empty paths, and
  unsupported entry kinds are dropped. Child directories that Companion cannot
  list are skipped, while failure to list the workspace root remains an error.
- Recursive file path scans must enforce bounded file, directory, and entry
  counts and report `truncated=true` when a bound stops traversal.
