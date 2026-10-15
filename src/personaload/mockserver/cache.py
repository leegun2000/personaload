"""Token counting and message-boundary prefix cache for the mock server (SPEC_B §11).

Every role counts toward prompt tokens (AIPerf's own mock counts user messages only, which
hides the persona and history cost this benchmark is about).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Any

CHARS_PER_TOKEN = 4

Message = dict[str, Any]


def message_text(message: Message) -> str:
    """Text of a chat message: a plain string or the text parts of a content list."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def message_tokens(message: Message) -> int:
    return math.ceil(len(message_text(message)) / CHARS_PER_TOKEN)


def prefix_digests(messages: Sequence[Message]) -> list[str]:
    """sha256 of messages[:k] for k = 1..n, over (role, text) only."""
    running = hashlib.sha256()
    digests = []
    for message in messages:
        canonical = [message.get("role"), message_text(message)]
        running.update(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode())
        running.update(b"\x1e")
        digests.append(running.copy().hexdigest())
    return digests


class PrefixCache:
    """Process-wide set of seen prefixes. No eviction: the mock is for tests and local runs."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def __len__(self) -> int:
        return len(self._seen)

    def cached_tokens(self, messages: Sequence[Message], digests: Sequence[str]) -> int:
        """Tokens of the longest message-boundary prefix seen before."""
        hit = max((k + 1 for k, d in enumerate(digests) if d in self._seen), default=0)
        return sum(message_tokens(m) for m in messages[:hit])

    def register(self, digests: Sequence[str]) -> None:
        self._seen.update(digests)
