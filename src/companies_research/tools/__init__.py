"""Tool harness — every capability the agent has, behind six gates."""

from __future__ import annotations

from .builtin import (
    calendar_read,
    deliver_brief,
    gmail_read,
    list_drive_files,
    read_drive_file,
    save_memory,
    search_memory,
    store_write,
    web_fetch,
    web_search,
)
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
    recipient_check,
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
    "calendar_read",
    "canonical_args_hash",
    "deliver_brief",
    "describe_registry",
    "gmail_read",
    "granted",
    "list_drive_files",
    "read_drive_file",
    "recipient_check",
    "save_memory",
    "search_memory",
    "set_caller",
    "store_write",
    "tool",
    "web_fetch",
    "web_search",
]
