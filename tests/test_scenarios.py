"""The shipped example scenarios stay valid as the schema evolves (SPEC_B §14 B5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from personaload.config import load_scenario
from personaload.workload.estimate import check_scenario
from personaload.workload.personas import load_personas

SCENARIOS = sorted((Path(__file__).parent.parent / "scenarios").glob("*.yaml"))


def test_the_examples_ship():
    names = {p.stem for p in SCENARIOS}
    assert names == {"smoke", "persona-shared", "persona-diverse", "long-context", "rag-assistant"}


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_example_validates_without_errors_or_warnings(path):
    scenario = load_scenario(path)
    personas = load_personas(scenario.personas, scenario.seed, scenario.session.language)
    checks = check_scenario(scenario, personas)
    assert not checks.errors and not checks.warnings, (checks.errors, checks.warnings)
