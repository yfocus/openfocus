<!-- SPDX-License-Identifier: Apache-2.0 -->
# Companion Domain SPEC

## Responsibility

The Companion domain owns OpenFocus-side companion device state and pairing rules:

- register and identify Companion devices by stable server-issued `companion_id`
- track online/offline/pending pairing state from the gRPC control connection
- validate pairing attempts and persist successful pairing credentials
- expose Companion capability state for AgentSpace, Inspiration terminal, runtime hook, and system inbox flows
- manage the selected System Inbox target Companion

## Boundaries

- `openfocus.companion` owns the local Companion runtime process and gRPC client implementation.
- The Companion domain owns persisted server-side state and product rules around trust, status, capabilities, and pairing.
- Companion must not expose a browser-facing HTTP server; browser actions go through OpenFocus Web/API routes.
- Terminal lifecycle ownership belongs to `openfocus/domains/agent_spaces/`.
- Runtime activity projection belongs to `openfocus/domains/agent_activity/`.

## Invariants

- A Companion starts as `pending_certification` until the user completes pairing.
- Pairing codes are 10 letters or digits and are rate-limited to 10 attempts per minute per Companion.
- Only paired and active Companions may be selected as System Inbox targets.
- Capability checks are required before issuing system float ball or terminal commands.
- Disconnects must promptly mark the device offline without deleting historical state.
