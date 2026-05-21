# SPDX-License-Identifier: Apache-2.0
"""Shared remote terminal domain interface."""

from .gateway import (
    RemoteTerminalGateway,
    TerminalGatewayError,
    TerminalInputError,
    TerminalMouseModeError,
    TerminalStartError,
    TerminalUnavailable,
    TerminalValidationError,
    maybe_inject_ttyd_bridge,
    terminal_payload,
    ttyd_bridge_script,
    ttyd_embed_path,
    ttyd_target_url,
)

__all__ = [
    "RemoteTerminalGateway",
    "TerminalGatewayError",
    "TerminalInputError",
    "TerminalMouseModeError",
    "TerminalStartError",
    "TerminalUnavailable",
    "TerminalValidationError",
    "maybe_inject_ttyd_bridge",
    "terminal_payload",
    "ttyd_bridge_script",
    "ttyd_embed_path",
    "ttyd_target_url",
]
