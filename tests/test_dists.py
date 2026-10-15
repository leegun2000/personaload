from __future__ import annotations

import math
import random
import statistics
from collections import Counter

import pytest

from personaload.config import CountDist, DurationDist
from personaload.workload.dists import (
    PersonaPicker,
    clamp,
    clamped_mean,
    configured_mean,
    draw,
    draw_int,
)
from personaload.workload.rng import child_rng, child_seed

N = 40_000


def _samples(spec, fn, n=N, seed=7):
    rng = random.Random(seed)
    return [fn(spec, rng) for _ in range(n)]


def test_child_seed_is_stable_and_component_scoped():
    assert child_seed(42, "session", 1, 3) == child_seed(42, "session", 1, 3)
    assert child_seed(42, "session", 1, 3) != child_seed(42, "session", 1, 4)
    assert child_seed(42, "persona", 0) != child_seed(43, "persona", 0)
    # pinned value: guards against accidental changes to the derivation
    assert child_seed(0, "x") == int.from_bytes(
        __import__("hashlib").sha256(b"0:x").digest()[:8], "big"
    )
    assert child_rng(1, "a").random() == child_rng(1, "a").random()


def test_clamp_open_bounds():
    assert clamp(5, None, None) == 5
    assert clamp(-1, 0, None) == 0
    assert clamp(9, None, 3) == 3


def test_uniform_int_covers_range_evenly():
    spec = CountDist(dist="uniform", min=5, max=30)
    xs = _samples(spec, draw_int)
    assert min(xs) == 5 and max(xs) == 30
    assert set(xs) == set(range(5, 31))
    assert statistics.fmean(xs) == pytest.approx(clamped_mean(spec), rel=0.01)


def test_uniform_duration_is_continuous():
    spec = DurationDist(dist="uniform", min="1s", max="3s")
    xs = _samples(spec, draw)
    assert all(1000 <= x <= 3000 for x in xs)
    assert len(set(xs)) > N * 0.99
    assert statistics.fmean(xs) == pytest.approx(2000, rel=0.01)


def test_constant():
    spec = CountDist(dist="constant", value=7)
    assert _samples(spec, draw_int, n=10) == [7] * 10
    assert clamped_mean(spec) == 7


def test_lognormal_parameterization_matches_mean_and_stddev():
    spec = CountDist(dist="lognormal", mean=50, stddev=30)
    xs = _samples(spec, draw)
    assert statistics.fmean(xs) == pytest.approx(50, rel=0.03)
    assert statistics.pstdev(xs) == pytest.approx(30, rel=0.06)


def test_lognormal_zero_stddev_is_constant():
    spec = CountDist(dist="lognormal", mean=50, stddev=0, min=1)
    assert set(_samples(spec, draw, n=100)) == {50}
    assert clamped_mean(spec) == 50


@pytest.mark.parametrize(
    "spec",
    [
        CountDist(dist="lognormal", mean=50, stddev=30, min=5, max=60),
        CountDist(dist="lognormal", mean=50, stddev=30, max=40),
        CountDist(dist="lognormal", mean=50, stddev=30, min=45),
        DurationDist(dist="exponential", mean="10s", min="1s", max="60s"),
        DurationDist(dist="exponential", mean="30s", min="1s", max="60s"),
        DurationDist(dist="exponential", mean="10s", max="5s"),
        DurationDist(dist="exponential", mean="10s", min="2s"),
    ],
)
def test_clamped_mean_matches_monte_carlo_and_bounds(spec):
    xs = _samples(spec, draw)
    lo = spec.min if spec.min is not None else 0
    hi = spec.max if spec.max is not None else math.inf
    assert all(lo <= x <= hi for x in xs)
    assert statistics.fmean(xs) == pytest.approx(clamped_mean(spec), rel=0.02)


def test_exponential_clamped_mean_known_value():
    # SPEC_B review: exponential mean 30s on [1s, 60s] has a clamped mean of ≈ 25.96s
    spec = DurationDist(dist="exponential", mean="30s", min="1s", max="60s")
    assert clamped_mean(spec) == pytest.approx(25_960, rel=0.001)


def test_draw_int_rounds_half_up_after_clamp():
    spec = CountDist(dist="exponential", mean=3, min=1, max=10)
    xs = _samples(spec, draw_int)
    assert min(xs) == 1 and max(xs) == 10
    assert all(isinstance(x, int) for x in xs)


def test_configured_mean():
    assert configured_mean(CountDist(dist="exponential", mean=3)) == 3
    assert configured_mean(CountDist(dist="uniform", min=1, max=3)) is None


def test_zipf_frequencies_follow_rank_weights():
    picker = PersonaPicker(10, "zipf", 1.1)
    probs = picker.probabilities()
    assert sum(probs) == pytest.approx(1)
    assert all(a > b for a, b in zip(probs, probs[1:], strict=False))
    rng = random.Random(3)
    counts = Counter(picker.pick(rng) for _ in range(N))
    for index, p in enumerate(probs):
        assert counts[index] / N == pytest.approx(p, abs=0.01)


def test_uniform_picker():
    picker = PersonaPicker(4, "uniform", 1.0)
    assert picker.probabilities() == [0.25] * 4
    rng = random.Random(5)
    counts = Counter(picker.pick(rng) for _ in range(N))
    assert set(counts) == {0, 1, 2, 3}
    assert all(c / N == pytest.approx(0.25, abs=0.01) for c in counts.values())


def test_single_persona():
    picker = PersonaPicker(1, "zipf", 1.1)
    rng = random.Random(1)
    assert {picker.pick(rng) for _ in range(100)} == {0}


# --- golden values: pin the exact §6.2 formulas, not just the distribution --------------


def test_lognormal_golden_box_muller_cosine_u1_then_u2():
    spec = CountDist(dist="lognormal", mean=50, stddev=30)
    rng = random.Random(123)
    u1, u2 = rng.random(), rng.random()
    sigma = math.sqrt(math.log(1 + (30 / 50) ** 2))
    mu = math.log(50) - sigma**2 / 2
    expected = math.exp(mu + sigma * math.sqrt(-2 * math.log(1 - u1)) * math.cos(2 * math.pi * u2))
    assert draw(spec, random.Random(123)) == expected


def test_exponential_golden():
    rng = random.Random(9)
    u = rng.random()
    assert draw(CountDist(dist="exponential", mean=7), random.Random(9)) == -7 * math.log(1 - u)


def test_uniform_int_golden():
    rng = random.Random(4)
    u = rng.random()
    assert draw_int(CountDist(dist="uniform", min=5, max=30), random.Random(4)) == 5 + math.floor(
        u * 26
    )


def test_draw_int_rounds_half_up_not_to_even():
    spec = DurationDist(dist="constant", value="2.5ms")
    assert draw_int(spec, random.Random(0)) == 3  # round() would give 2


def test_degenerate_lognormal_still_consumes_two_draws():
    rng = random.Random(1)
    draw(CountDist(dist="lognormal", mean=50, stddev=0, min=1), rng)
    reference = random.Random(1)
    reference.random(), reference.random()
    assert rng.random() == reference.random()


def test_zipf_golden_bisect():
    picker = PersonaPicker(5, "zipf", 1.3)
    rng = random.Random(11)
    u = rng.random()
    weights = [r**-1.3 for r in range(1, 6)]
    cum = [sum(weights[: i + 1]) for i in range(5)]
    expected = next(i for i, c in enumerate(cum) if c > u * cum[-1])
    assert picker.pick(random.Random(11)) == expected
