<!-- SPDX-License-Identifier: Apache-2.0 -->
# Attention Inbox Domain

The attention domain turns high-value events into persistent, user-actionable items.

- Only completion, failed, or blocked events create attention items.
- Active items remain visible until the user explicitly dismisses them or takes the primary action.
- Task actions prefer AgentSpace when present and fall back to the Dashboard task anchor.
- Low-value progress events remain in the Events timeline and do not enter the inbox.
