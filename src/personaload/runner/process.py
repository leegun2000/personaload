"""Running one `aiperf profile` process: 2-stage watchdog and Ctrl+C forwarding (SPEC_B §8.3, §8.5).

AIPerf runs in its own session/process group, so a terminal Ctrl+C reaches only personaload.
personaload then forwards exactly one SIGINT (AIPerf treats a second SIGINT as force-quit and
may drop results). A second Ctrl+C escalates to SIGKILL.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PROFILING_MARK = "AIPerf System is PROFILING"
STARTUP_TIMEOUT_S = 600.0
RUN_SLACK_S = 120.0
KILL_GRACE_S = 15.0
POLL_S = 0.2


class Interrupts:
    """Ctrl+C counter shared between the signal handler and the watch loop."""

    def __init__(self) -> None:
        self.count = 0

    def handle(self, signum: int, frame: object) -> None:
        self.count += 1


@dataclass(frozen=True)
class Outcome:
    exit_code: int | None
    watchdog: str | None  # why the watchdog stopped the process, if it did
    interrupted: bool
    t_stop_ns: int | None  # wall-clock ns when personaload asked AIPerf to stop


def _signal_group(pid: int, sig: int) -> None:
    # group already gone; macOS raises EPERM when only zombies are left in it
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _reached_profiling(log: Path) -> bool:
    try:
        return PROFILING_MARK in log.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return False


def run_watched(
    cmd: list[str],
    env: dict[str, str],
    log: Path,
    console: Path,
    run_budget_s: float,
    interrupts: Interrupts,
    *,
    startup_timeout_s: float = STARTUP_TIMEOUT_S,
    kill_grace_s: float = KILL_GRACE_S,
) -> Outcome:
    """Run `cmd` until it exits. `log` is AIPerf's logs/aiperf.log (the PROFILING marker source);
    `run_budget_s` starts counting once that marker appears."""
    with console.open("wb") as out:
        proc = subprocess.Popen(
            cmd, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True
        )
    started = time.monotonic()
    run_deadline: float | None = None
    kill_at: float | None = None
    watchdog: str | None = None
    t_stop: int | None = None
    sent_sigint = sent_sigkill = False

    def stop(reason: str | None) -> None:
        nonlocal sent_sigint, kill_at, t_stop, watchdog
        _signal_group(proc.pid, signal.SIGINT)  # exactly once: callers check sent_sigint
        sent_sigint, t_stop, watchdog = True, time.time_ns(), reason
        if reason:  # watchdog only; a user escalates with a second Ctrl+C (§8.5)
            kill_at = time.monotonic() + kill_grace_s

    while proc.poll() is None:
        now = time.monotonic()
        if run_deadline is None and _reached_profiling(log):
            run_deadline = now + run_budget_s
        if interrupts.count >= 2 and not sent_sigkill:
            _signal_group(proc.pid, signal.SIGKILL)
            sent_sigkill = True
            t_stop = t_stop or time.time_ns()  # both presses in one poll: no SIGINT was sent
        elif not sent_sigint:
            if interrupts.count >= 1:
                stop(None)
            elif run_deadline is None and now - started > startup_timeout_s:
                stop(
                    "startup watchdog: AIPerf did not reach PROFILING "
                    f"within {startup_timeout_s:g}s"
                )
            elif run_deadline is not None and now > run_deadline:
                stop(f"run watchdog: more than {run_budget_s:g}s since PROFILING started")
        if kill_at is not None and now > kill_at and not sent_sigkill:
            _signal_group(proc.pid, signal.SIGKILL)
            sent_sigkill = True
        time.sleep(POLL_S)
    _signal_group(proc.pid, signal.SIGKILL)  # reap leftover AIPerf services in the group
    # a Ctrl+C that lands after AIPerf exited on its own did not interrupt it
    return Outcome(proc.returncode, watchdog, t_stop is not None and watchdog is None, t_stop)
