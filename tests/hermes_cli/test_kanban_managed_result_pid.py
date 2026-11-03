"""A managed Kanban run's lost-ACK recovery honours the exit its authority recorded.

``tasks.worker_pid`` is the dispatcher's spawned submitter, but the managed interpreter that ran
the turns lives under the profile authority and writes ``worker_result`` with ITS pid (the one
``worker_bound`` recorded). The dead-worker sweep must accept that result under the same claim,
so a quota wall (exit 75) requeues as rate-limited instead of booking a crash.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.mark.parametrize("bound_claim, expected", [("own", "rate_limited"), ("foreign", "crashed")])
def test_managed_exit_75_requeues_rate_limited_not_crashed(conn, bound_claim, expected):
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="managed", assignee="w")
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None
    submitter, interpreter = _dead_pid(), _dead_pid()
    assert submitter != interpreter
    kbd._set_worker_pid(conn, tid, submitter)
    claim = task.claim_lock if bound_claim == "own" else "someone-else"
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "worker_bound", {"pid": interpreter, "claim_lock": claim, "started_at": "x"},
                         run_id=task.current_run_id)
        kb._append_event(conn, tid, "worker_result", {"pid": interpreter, "claim_lock": task.claim_lock,
                         "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE, "last_output": ""}, run_id=task.current_run_id)

    crashed = kbd.detect_crashed_workers(conn)

    outcome = conn.execute("SELECT outcome FROM task_runs WHERE id=?", (task.current_run_id,)).fetchone()[0]
    assert outcome == expected
    assert (tid in kbd.detect_crashed_workers._last_rate_limited) is (expected == "rate_limited")
    assert (tid in crashed) is (expected == "crashed")


def test_rate_limit_reset_survives_managed_reclaim(conn):
    import json
    import time

    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="reset-aware retry", assignee="w")
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None

    submitter, interpreter = _dead_pid(), _dead_pid()
    kbd._set_worker_pid(conn, tid, submitter)

    reset_at = time.time() + 3600

    with kb.write_txn(conn):
        kb._append_event(
            conn, tid, "worker_bound",
            {"pid": interpreter, "claim_lock": task.claim_lock},
            run_id=task.current_run_id,
        )
        kb._append_event(
            conn, tid, "worker_result",
            {
                "pid": interpreter,
                "claim_lock": task.claim_lock,
                "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE,
                "last_output": "",
                "retry": {
                    "version": 1,
                    "reason": "rate_limit",
                    "not_before": reset_at,
                    "deadline_source": "provider_reset",
                },
            },
            run_id=task.current_run_id,
        )

    kbd.detect_crashed_workers(conn)

    row = conn.execute(
        "SELECT outcome, metadata FROM task_runs WHERE id=?",
        (task.current_run_id,),
    ).fetchone()

    assert row["outcome"] == "rate_limited"
    metadata = json.loads(row["metadata"])
    assert metadata["retry"]["not_before"] == reset_at


def test_rate_limit_guard_respects_provider_reset(conn, monkeypatch):
    import time

    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="reset guard", assignee="w")
    task = kb.claim_task(conn, tid, claimer=f"{host}:owner")
    assert task is not None

    submitter, interpreter = _dead_pid(), _dead_pid()
    kbd._set_worker_pid(conn, tid, submitter)

    ended_at = int(time.time())
    reset_at = ended_at + 3600

    with kb.write_txn(conn):
        kb._append_event(
            conn, tid, "worker_bound",
            {"pid": interpreter, "claim_lock": task.claim_lock},
            run_id=task.current_run_id,
        )
        kb._append_event(
            conn, tid, "worker_result",
            {
                "pid": interpreter,
                "claim_lock": task.claim_lock,
                "exit_code": kb.KANBAN_RATE_LIMIT_EXIT_CODE,
                "last_output": "",
                "retry": {
                    "version": 1,
                    "reason": "rate_limit",
                    "not_before": reset_at,
                    "deadline_source": "provider_reset",
                },
            },
            run_id=task.current_run_id,
        )

    kbd.detect_crashed_workers(conn)

    monkeypatch.setattr(kbd.time, "time", lambda: ended_at + 301)
    assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"

    monkeypatch.setattr(kbd.time, "time", lambda: reset_at + 11)
    assert kbd.check_respawn_guard(conn, tid) is None
