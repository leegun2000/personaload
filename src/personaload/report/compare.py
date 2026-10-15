"""`personaload report <dir>...`: compare results directories from stage.json alone (SPEC_B §9.7).

No re-aggregation, so directories written with `record_requests: false` compare the same way.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from personaload.report.judge import headline
from personaload.report.render import headline_text, stage_table


class ReportError(Exception):
    pass


def load_run(run_dir: Path) -> list[dict]:
    paths = sorted(Path(run_dir).glob("stage_*/stage.json"))
    if not paths:
        raise ReportError(f"{run_dir}: stage_*/stage.json이 없다")
    try:
        stages = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"{run_dir}: stage.json을 읽지 못했다: {exc}") from exc
    for path, stage in zip(paths, stages, strict=True):
        if not isinstance(stage, dict) or not isinstance(stage.get("stage"), int):
            raise ReportError(f"{path}: stage.json 형식이 아니다")
    return stages


def workload_warnings(runs: dict[str, list[dict]]) -> list[str]:
    """Stage k of every run must replay the same dataset to be comparable."""
    digests: dict[int, set[str | None]] = {}
    for stages in runs.values():
        for s in stages:
            digests.setdefault(s["stage"], set()).add(s.get("dataset_sha256"))
    return [f"단계 {k}의 워크로드가 다름" for k, found in sorted(digests.items()) if len(found) > 1]


def load_runs(run_dirs: Sequence[Path]) -> dict[str, tuple[Path, list[dict]]]:
    """Run label -> (absolute directory, stage.json dicts), in the given order."""
    paths = [Path(d).resolve() for d in run_dirs]  # `personaload report .` needs a real name
    names = [p.name for p in paths]
    if len(set(names)) < len(names):  # same run name under different parents: show paths
        names = [str(p) for p in paths]
    return {name: (path, load_run(path)) for name, path in zip(names, paths, strict=True)}


def compare_markdown(run_dirs: Sequence[Path]) -> str:
    runs = {name: stages for name, (_, stages) in load_runs(run_dirs).items()}
    parts = ["# personaload 비교"]
    parts += [f"> 경고: {w}" for w in workload_warnings(runs)]
    parts.append("## 헤드라인")
    parts.append("\n".join(f"- {name}: {headline_text(headline(st))}" for name, st in runs.items()))
    parts.append(stage_table([(name, s) for name, stages in runs.items() for s in stages]))
    return "\n\n".join(parts) + "\n"
