"""Distribution sampling and clamped expectations (SPEC_B §6.2).

Every transform consumes `rng.random()` only, so sequences stay stable across Python
versions regardless of how the stdlib implements its own distribution helpers.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from itertools import accumulate
from statistics import NormalDist

from personaload.config import Dist

_PHI = NormalDist().cdf


def clamp(x: float, lo: float | None, hi: float | None) -> float:
    if lo is not None:
        x = max(x, lo)
    if hi is not None:
        x = min(x, hi)
    return x


def _lognormal_params(mean: float, stddev: float) -> tuple[float, float]:
    sigma2 = math.log(1 + (stddev / mean) ** 2)
    return math.log(mean) - sigma2 / 2, math.sqrt(sigma2)


def draw(spec: Dist, rng: random.Random) -> float:
    """Continuous draw, clamped. uniform is continuous on [min, max]."""
    if spec.dist == "uniform":
        x = spec.min + rng.random() * (spec.max - spec.min)
    elif spec.dist == "constant":
        x = spec.value
    elif spec.dist == "exponential":
        x = -spec.mean * math.log(1 - rng.random())
    else:  # lognormal: Box-Muller cosine branch, sine branch discarded
        u1, u2 = rng.random(), rng.random()  # consumed even when degenerate (§6.2 stream order)
        if spec.stddev == 0:
            x = spec.mean  # exp(ln m) would drift by one ulp
        else:
            mu, sigma = _lognormal_params(spec.mean, spec.stddev)
            z = math.sqrt(-2 * math.log(1 - u1)) * math.cos(2 * math.pi * u2)
            x = math.exp(mu + sigma * z)
    return clamp(x, spec.min, spec.max)


def draw_int(spec: Dist, rng: random.Random) -> int:
    """Integer draw: uniform picks an integer in [min, max]; others round half up after clamping."""
    if spec.dist == "uniform":
        lo, hi = int(spec.min), int(spec.max)
        return lo + math.floor(rng.random() * (hi - lo + 1))
    return math.floor(draw(spec, rng) + 0.5)


def clamped_mean(spec: Dist) -> float:
    """E[clamp(X, min, max)] in closed form."""
    lo, hi = spec.min, spec.max
    if spec.dist == "uniform":
        return (lo + hi) / 2
    if spec.dist == "constant":
        return clamp(spec.value, lo, hi)
    if spec.dist == "exponential":
        a, m = lo or 0.0, spec.mean
        tail = math.exp(-hi / m) if hi is not None else 0.0
        return a + m * (math.exp(-a / m) - tail)
    mu, sigma = _lognormal_params(spec.mean, spec.stddev)
    if sigma == 0:
        return clamp(spec.mean, lo, hi)
    has_lo = lo is not None and lo > 0
    alpha = (math.log(lo) - mu) / sigma if has_lo else -math.inf
    beta = (math.log(hi) - mu) / sigma if hi is not None else math.inf
    below = lo * _PHI(alpha) if has_lo else 0.0
    above = hi * (1 - _PHI(beta)) if hi is not None else 0.0
    inside = math.exp(mu + sigma**2 / 2) * (
        _phi_or_limit(beta - sigma) - _phi_or_limit(alpha - sigma)
    )
    return below + inside + above


def _phi_or_limit(x: float) -> float:
    if x == math.inf:
        return 1.0
    if x == -math.inf:
        return 0.0
    return _PHI(x)


def configured_mean(spec: Dist) -> float | None:
    """The mean the user wrote, where the distribution has one."""
    return spec.mean if spec.dist in ("lognormal", "exponential") else None


class PersonaPicker:
    """Chooses a persona index: uniform, or Zipf with p(rank) ∝ rank^-s (rank 1 = index 0)."""

    def __init__(self, count: int, selection: str, zipf_s: float):
        self.count = count
        weights = [r**-zipf_s for r in range(1, count + 1)] if selection == "zipf" else None
        self._cum = list(accumulate(weights)) if weights else None

    def pick(self, rng: random.Random) -> int:
        u = rng.random()
        if self._cum is None:
            return math.floor(u * self.count)
        return bisect_right(self._cum, u * self._cum[-1], 0, self.count - 1)

    def probabilities(self) -> list[float]:
        if self._cum is None:
            return [1 / self.count] * self.count
        total = self._cum[-1]
        prev = [0.0, *self._cum[:-1]]
        return [(c - p) / total for c, p in zip(self._cum, prev, strict=True)]
