"""Tool harness — every capability the agent has, behind six gates."""

from __future__ import annotations

from .builtin import gmail_read, store_write, web_fetch, web_search
from .registry import (
    GATES,
    REGISTRY,
    ToolCallRecord,
    ToolDenied,
    ToolSpec,
    add_scope_check,
    caller,
    canonical_args_hash,
    describe_registry,
    granted,
    set_caller,
    tool,
)

__all__ = [
    "GATES",
    "REGISTRY",
    "ToolCallRecord",
    "ToolDenied",
    "ToolSpec",
    "add_scope_check",
    "caller",
    "canonical_args_hash",
    "describe_registry",
    "gmail_read",
    "granted",
    "set_caller",
    "store_write",
    "tool",
    "web_fetch",
    "web_search",
]
