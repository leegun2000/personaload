"""Per-component random streams derived from the scenario seed (SPEC_B §7.1).

Each component gets its own stream keyed by a SHA-256 of "seed:part:part...", so adding
a component or reordering draws in one place never shifts another component's sequence.
Only `Random.random()` is used downstream; distribution transforms live in `dists`.
"""

from __future__ import annotations

import hashlib
import random


def child_seed(seed: int, *parts: object) -> int:
    key = ":".join(str(p) for p in (seed, *parts)).encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big")


def child_rng(seed: int, *parts: object) -> random.Random:
    return random.Random(child_seed(seed, *parts))
