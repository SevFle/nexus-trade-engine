"""Cursor-based pagination and token-budget result guarding.

Large engine responses (full market-data history, long order logs) can easily
exceed an LLM's context window or the MCP message-size soft cap. Two helpers
keep responses safe:

* :func:`paginate` — slices a list using an opaque base64 cursor that encodes
  the integer offset. The caller receives the page plus the next cursor (or
  ``None`` when exhausted).
* :class:`ResultGuard` — estimates the token cost of a payload (~4 chars per
  token) and, when it exceeds the configured budget, truncates list payloads
  and records that truncation occurred.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from engine.mcp.config import mcp_settings

_CHARS_PER_TOKEN = 4


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return max(0, int(base64.urlsafe_b64decode(padded).decode()))
    except (ValueError, UnicodeDecodeError):
        return 0


@dataclass
class Page:
    items: list[Any]
    next_cursor: str | None
    total: int
    limit: int
    offset: int


def paginate(
    items: list[Any],
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> Page:
    """Slice ``items`` into a single :class:`Page` from ``cursor``."""
    max_page = mcp_settings.max_page_size
    page_size = mcp_settings.default_page_size
    effective_limit = max(1, min(limit or page_size, page_size, max_page))
    offset = decode_cursor(cursor)
    if offset < 0 or offset > len(items):
        offset = 0
    end = offset + effective_limit
    chunk = items[offset:end]
    next_cursor = encode_cursor(end) if end < len(items) else None
    return Page(
        items=chunk,
        next_cursor=next_cursor,
        total=len(items),
        limit=effective_limit,
        offset=offset,
    )


def page_to_dict(page: Page, *, items_key: str = "items") -> dict[str, Any]:
    return {
        items_key: page.items,
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
        "next_cursor": page.next_cursor,
    }


class ResultGuard:
    """Token-budget aware truncation for list-heavy payloads.

    Estimates payload size via JSON serialisation + char/token heuristic. When
    the estimate exceeds ``token_budget`` the largest list value is trimmed to
    fit and a ``truncated`` flag is attached so the assistant knows more data
    is available via pagination.
    """

    def __init__(self, token_budget: int | None = None) -> None:
        self.token_budget = token_budget or mcp_settings.result_token_budget

    def estimate_tokens(self, payload: Any) -> int:
        try:
            encoded = json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            encoded = str(payload)
        return len(encoded) // _CHARS_PER_TOKEN

    def guard(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return ``payload`` possibly truncated to fit the token budget."""
        if self.estimate_tokens(payload) <= self.token_budget:
            return payload

        result = dict(payload)
        # Trim the longest list-valued field first (usually 'bars'/'orders').
        list_keys = [k for k, v in result.items() if isinstance(v, list)]
        list_keys.sort(key=lambda k: len(result[k]), reverse=True)

        for key in list_keys:
            if self.estimate_tokens(result) <= self.token_budget:
                break
            items = result[key]
            if not items:
                continue
            # Binary-search the largest slice that fits the budget.
            lo, hi = 0, len(items)
            best = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                candidate = dict(result)
                candidate[key] = items[:mid]
                if self.estimate_tokens(candidate) <= self.token_budget:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            result[key] = items[:best]
            result[f"{key}_truncated"] = True
            result[f"{key}_total"] = len(items)

        result["truncated"] = True
        result["token_budget"] = self.token_budget
        return result


__all__ = [
    "Page",
    "ResultGuard",
    "decode_cursor",
    "encode_cursor",
    "page_to_dict",
    "paginate",
]
