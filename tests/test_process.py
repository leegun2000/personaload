"""Watchdog and Ctrl+C forwarding against fake processes (SPEC_B §8.3, §8.5)."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from personaload.runner import process
from personaload.runner.process import PROFILING_MARK, Interrupts, run_watched

SLEEP = "import time; time.sleep(60)"
MARK_THEN_SLEEP = (
    "import pathlib, sys, time; p = pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True);"
    f" p.write_text({PROFILING_MARK!r}); time.sleep(60)"
)
IGNORE_SIGINT = "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)"
# counts SIGINTs for 1 s after the first one, then exits with the count
COUNT_SIGINT = (
    "import signal, sys, time; n = [0]; signal.signal(signal.SIGINT, lambda *a: n.append(0))\n"
    "while len(n) == 1: time.sleep(0.01)\n"
    "time.sleep(1.0); sys.exit(len(n) - 1)"
)
# leader forks C (own group) which forks D back into the leader's group; D stays a zombie
# there after the leader exits, and killpg on that group raises EPERM on macOS
ZOMBIE_IN_GROUP = (
    "import os, sys, time\n"
    "g = os.getpgrp(); c = os.fork()\n"
    "if c == 0:\n"
    "    os.setpgid(0, 0)\n"
    "    if os.fork() == 0: os.setpgid(0, g); os._exit(0)\n"
    "    time.sleep(3); os._exit(0)\n"
    "time.sleep(0.3); os._exit(0)"
)


def _run(tmp_path: Path, code: str, *, interrupts=None, budget=30.0, startup=30.0, grace=0.5):
    log = tmp_path / "aiperf" / "logs" / "aiperf.log"
    started = time.monotonic()
    outcome = run_watched(
        [sys.executable, "-c", code, str(log)],
        dict(os.environ),
        log,
        tmp_path / "console.log",
        budget,
        interrupts or Interrupts(),
        startup_timeout_s=startup,
        kill_grace_s=grace,
    )
    return outcome, time.monotonic() - started


def _press_ctrl_c(interrupts: Interrupts, times: int, after: float) -> None:
    def press():
        time.sleep(after)
        interrupts.count += times

    threading.Thread(target=press, daemon=True).start()


def test_normal_exit_passes_exit_code_through(tmp_path):
    outcome, _ = _run(tmp_path, "raise SystemExit(3)")
    assert outcome.exit_code == 3
    assert outcome.watchdog is None and not outcome.interrupted and outcome.t_stop_ns is None


def test_startup_watchdog(tmp_path):
    outcome, elapsed = _run(tmp_path, SLEEP, startup=0.5)
    assert outcome.watchdog.startswith("startup watchdog")
    assert outcome.t_stop_ns is not None and elapsed < 5


def test_run_watchdog_counts_from_profiling_mark(tmp_path):
    outcome, elapsed = _run(tmp_path, MARK_THEN_SLEEP, budget=0.5, startup=0.3)
    assert outcome.watchdog.startswith("run watchdog")  # startup 0.3 s did not fire: mark was seen
    assert elapsed < 5


def test_watchdog_escalates_to_sigkill(tmp_path):
    outcome, elapsed = _run(tmp_path, IGNORE_SIGINT, startup=0.3, grace=0.5)
    assert outcome.exit_code == -9 and outcome.watchdog is not None
    assert elapsed < 5


def test_first_ctrl_c_forwards_exactly_one_sigint_and_never_kills(tmp_path):
    interrupts = Interrupts()
    _press_ctrl_c(interrupts, 1, after=0.5)
    outcome, _ = _run(tmp_path, COUNT_SIGINT, interrupts=interrupts, grace=0.3)
    assert outcome.exit_code == 1  # one SIGINT, and no SIGKILL although grace (0.3 s) < 1 s
    assert outcome.interrupted and outcome.watchdog is None and outcome.t_stop_ns is not None


def test_double_ctrl_c_within_one_poll_records_t_stop(tmp_path):
    interrupts = Interrupts()
    interrupts.count = 2
    outcome, _ = _run(tmp_path, IGNORE_SIGINT, interrupts=interrupts, grace=30)
    assert outcome.exit_code == -9 and outcome.interrupted and outcome.t_stop_ns is not None


def test_ctrl_c_after_exit_does_not_mark_interrupted(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "POLL_S", 1.5)  # exit at ~0.1 s, Ctrl+C at 0.8 s, poll at 1.5 s
    interrupts = Interrupts()
    _press_ctrl_c(interrupts, 1, after=0.8)
    outcome, _ = _run(tmp_path, "raise SystemExit(3)", interrupts=interrupts)
    assert outcome.exit_code == 3 and not outcome.interrupted and outcome.t_stop_ns is None


def test_second_ctrl_c_kills(tmp_path):
    interrupts = Interrupts()
    _press_ctrl_c(interrupts, 2, after=0.5)
    outcome, elapsed = _run(tmp_path, IGNORE_SIGINT, interrupts=interrupts, grace=30)
    assert outcome.exit_code == -9 and elapsed < 5


def test_leftover_group_members_are_reaped(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys; child = subprocess.Popen(['sleep', '60']);"
        f" open({str(pid_file)!r}, 'w').write(str(child.pid))"
    )
    outcome, _ = _run(tmp_path, code)
    assert outcome.exit_code == 0
    child = int(pid_file.read_text())
    time.sleep(0.2)
    try:
        os.kill(child, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def test_zombie_only_group_does_not_raise(tmp_path):
    outcome, _ = _run(tmp_path, ZOMBIE_IN_GROUP)
    assert outcome.exit_code == 0
