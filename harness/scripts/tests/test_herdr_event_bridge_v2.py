"""Bridge v2: debounce, hold, per-workflow single-flight, watchdog, delivery.

Everything runs against fakes (pane list / pane read / agent get / delivery)
and a temporary SQLite ``sessions`` table; nothing touches ~/.hermes.
"""
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import herdr_event_bridge as bridge  # noqa: E402
import webhook_ids  # noqa: E402

CWD = "/srv/example-app"
T0 = 1_790_000_000.0
ACCEPTED = "accepted"


def reg_task(task_id, pane_id, **extra):
    task = {
        "id": task_id, "enabled": True, "session": "default", "workspace_id": "w5",
        "pane_id": pane_id, "agent": "claude", "project": "ExampleApp", "cwd": CWD,
        "change": "change", "phase": "apply", "policy": {"allow_continue": True},
    }
    task.update(extra)
    return task


def no_continue(task_id="a", pane_id="w5:p1", **extra):
    return reg_task(task_id, pane_id, policy={"allow_continue": False}, **extra)


class Harness:
    def __init__(self, tmp_path, tasks=None, panes=None, *, threaded=False, auto_end=False,
                 initial_status="working", state=None, **options):
        self.tmp = tmp_path
        self.registry_path = tmp_path / "state" / "herdr_tasks.json"
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path = tmp_path / "state" / "herdr_event_bridge_state.json"
        if state is not None:
            self.state_path.write_text(state if isinstance(state, str) else json.dumps(state))
        self.tasks = tasks if tasks is not None else [reg_task("a", "w5:p1")]
        self.write_registry()
        self.db_path = options.pop("state_db", tmp_path / "state.db")
        if not Path(self.db_path).exists() and Path(self.db_path).parent.exists():
            conn = sqlite3.connect(self.db_path)
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT, "
                         "started_at REAL, ended_at REAL, end_reason TEXT)")
            conn.commit()
            conn.close()
        self.panes = panes if panes is not None else [
            {"pane_id": t["pane_id"], "cwd": CWD, "agent_status": initial_status} for t in self.tasks
        ]
        self.pane_text = {}
        self.revision_changes = True
        self.revision = 0
        self.responses = []
        self.sent = []
        self.sent_at = []
        self.list_calls = 0
        self.auto_end = auto_end
        self.chat_format = options.pop("chat_format", "legacy")
        self.delivery_seq = 0
        self.now = T0
        cfg = bridge.BridgeConfig(self.registry_path, self.state_path, self.db_path,
                                  project="ExampleApp", cwd_prefix=[CWD], **options)
        self.bridge = bridge.Bridge(cfg, list_panes=self.list_panes, read_pane=self.read_pane,
                                    agent_get=self.agent_get, deliver=self.deliver,
                                    threaded=threaded, clock=lambda: self.now)
        assert self.bridge.prepare_subscription(self.now)
        self.bridge.mark_subscribed(self.now)

    # fakes ---------------------------------------------------------------
    def write_registry(self, raw=None):
        temp = self.registry_path.with_name("tasks.tmp")
        temp.write_text(raw if isinstance(raw, str) else json.dumps({"version": 1, "tasks": self.tasks}))
        temp.replace(self.registry_path)

    def list_panes(self):
        self.list_calls += 1
        return [dict(p) for p in self.panes]

    def read_pane(self, pane_id):
        return self.pane_text.get(pane_id, "")

    def agent_get(self, pane_id):
        if self.revision_changes:
            self.revision += 1
        return {"pane_id": pane_id, "revision": self.revision}

    def deliver(self, payload):
        self.sent.append(payload)
        self.sent_at.append(self.now)
        if self.responses:
            response = self.responses.pop(0)
            return response(payload) if callable(response) else response
        self.delivery_seq += 1
        delivery_id = f"d{self.delivery_seq}"
        if self.auto_end:
            self.controller(delivery_id, ended=self.now + 1)
        return bridge.DeliveryResult(ACCEPTED, delivery_id, "status=accepted")

    # controller rows in state.db ----------------------------------------------
    def controller(self, delivery_id, *, started=None, ended=None):
        legacy, v2 = webhook_ids.session_chat_ids(self.bridge.cfg.webhook, delivery_id, self.bridge.cfg.profile)
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?)",
                     (f"s-{delivery_id}", "webhook", v2 if self.chat_format == "v2" else legacy,
                      self.now if started is None else started, ended,
                      "webhook_complete" if ended else None))
        conn.commit()
        conn.close()

    def finish(self, delivery_id):
        self.controller(delivery_id, started=self.now - 1, ended=self.now)

    # driving -------------------------------------------------------------------
    def status(self, pane_id, status):
        self.bridge.handle_message(
            {"event": "pane.agent_status_changed", "data": {"pane_id": pane_id, "agent_status": status}},
            self.now)

    def advance(self, seconds, step=1.0):
        steps = int(round(seconds / step))
        for _ in range(steps):
            self.now += step
            self.bridge.tick(self.now)

    def reasons(self):
        return [p["reason"] for p in self.sent]

    def pane(self, task_id="a"):
        return self.bridge.state["panes"][task_id]


# --------------------------------------------------------------------------
# Debounce
# --------------------------------------------------------------------------


def test_debounce_latest_status_wins_and_reschedules(tmp_path):
    h = Harness(tmp_path)
    h.status("w5:p1", "idle")
    h.advance(10)
    h.status("w5:p1", "done")
    h.advance(15)  # the original idle wake (due at +20) must not fire
    assert h.sent == []
    h.advance(6)
    assert [(p["status"], p["reason"]) for p in h.sent] == [("done", "status")]
    assert h.sent[0]["event_id"] == "a:3:done"  # baseline working=1, idle=2, done=3


def test_idle_flap_produces_single_wake(tmp_path):
    h = Harness(tmp_path)
    for _ in range(7):
        h.status("w5:p1", "idle")
        h.advance(5)
        h.status("w5:p1", "unknown")
        h.advance(5)
    h.status("w5:p1", "idle")
    h.advance(25)
    assert len(h.sent) == 1


def test_working_cancels_scheduled_wake(tmp_path):
    h = Harness(tmp_path)
    h.status("w5:p1", "idle")
    h.advance(10)
    h.status("w5:p1", "working")
    h.advance(60)
    assert h.sent == []


def test_blocked_wakes_after_short_debounce(tmp_path):
    h = Harness(tmp_path)
    h.status("w5:p1", "blocked")
    h.advance(2)
    assert h.sent == []
    h.advance(1)
    assert [p["status"] for p in h.sent] == ["blocked"]


def test_unknown_waits_grace_and_is_replaced_by_newer_status(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "unknown")
    h.advance(44)
    assert h.sent == []
    h.advance(1)
    assert [p["status"] for p in h.sent] == ["unknown"]

    h2 = Harness(tmp_path / "second", tasks=[no_continue()])
    h2.status("w5:p1", "unknown")
    h2.advance(30)
    h2.status("w5:p1", "idle")
    h2.advance(19)
    assert h2.sent == []
    h2.advance(1)
    assert [p["status"] for p in h2.sent] == ["idle"]


def test_unknown_for_closing_pane_is_dropped(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "unknown")
    h.panes = []
    h.bridge.handle_message({"event": "pane.closed", "data": {"pane_id": "w5:p1"}}, h.now)
    h.advance(60)
    assert h.sent == []


def test_auto_task_never_wakes(tmp_path):
    # Unregistered panes are tracked but never woken: the route script drops them anyway.
    panes = [{"pane_id": "w8:p1", "cwd": CWD, "agent_status": "working"}]
    h = Harness(tmp_path, tasks=[], panes=panes)
    assert set(h.bridge.tasks) == {"auto:w8:p1"}
    for status in ("unknown", "idle", "done", "blocked"):
        h.status("w8:p1", status)
        h.advance(600, step=5)
    assert h.sent == []


# --------------------------------------------------------------------------
# Hold
# --------------------------------------------------------------------------


def test_hold_skips_repeat_without_new_work(tmp_path, capsys):
    h = Harness(tmp_path, tasks=[no_continue()], auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(30)
    assert len(h.sent) == 1
    h.status("w5:p1", "unknown")
    h.advance(5)
    h.status("w5:p1", "idle")
    h.advance(30)
    assert len(h.sent) == 1
    assert "held task=a status=idle" in capsys.readouterr().out
    # A new working epoch releases the hold.
    h.status("w5:p1", "working")
    h.advance(30)
    h.status("w5:p1", "idle")
    h.advance(21)
    assert len(h.sent) == 2


def test_hold_expires_and_never_applies_to_blocked(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()], auto_end=True)
    h.status("w5:p1", "blocked")
    h.advance(10)
    h.status("w5:p1", "unknown")
    h.advance(5)
    h.status("w5:p1", "blocked")
    h.advance(10)
    assert [p["status"] for p in h.sent] == ["blocked", "blocked"]
    h.status("w5:p1", "idle")
    h.advance(30)
    assert len(h.sent) == 3
    h.advance(900, step=10)  # hold window over
    h.status("w5:p1", "unknown")
    h.advance(5)
    h.status("w5:p1", "idle")
    h.advance(21)
    assert len(h.sent) == 4


# --------------------------------------------------------------------------
# Single-flight
# --------------------------------------------------------------------------


def test_single_flight_drops_self_caused_events(tmp_path, capsys):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "idle")
    h.advance(21)
    assert len(h.sent) == 1
    assert h.bridge.inflight("a")["delivery_id"] == "d1"
    h.controller("d1")  # controller running
    h.status("w5:p1", "working")  # its own /clear
    h.advance(3)
    h.status("w5:p1", "idle")
    h.advance(25)
    assert len(h.sent) == 1
    assert h.pane()["deferred"]["count"] == 1
    h.finish("d1")
    h.advance(6)
    assert len(h.sent) == 1
    assert h.bridge.inflight("a") is None
    assert h.pane()["deferred"] is None
    out = capsys.readouterr().out
    assert "deferred task=a" in out and "reason=self_caused" in out


def test_controller_end_found_for_v2_session_chat_ids(tmp_path, capsys):
    # Hermes 0.21.5+ keys webhook sessions as webhook:v2:<b64 [profile, route, delivery]>;
    # missing that row held every workflow until the no-row fallback.
    h = Harness(tmp_path, tasks=[no_continue()], profile="exampleapp", chat_format="v2")
    h.status("w5:p1", "idle")
    h.advance(21)
    assert len(h.sent) == 1
    h.controller("d1")
    h.advance(5)
    assert h.bridge.inflight("a") is not None
    h.finish("d1")
    h.advance(6)
    assert h.bridge.inflight("a") is None
    assert "how=db" in capsys.readouterr().out


def test_single_flight_wakes_for_real_work_after_controller(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "idle")
    h.advance(21)
    h.controller("d1")
    h.status("w5:p1", "working")
    h.advance(60)
    h.status("w5:p1", "done")
    h.advance(25)
    assert len(h.sent) == 1
    h.finish("d1")
    h.advance(6)
    assert len(h.sent) == 2
    wake = h.sent[1]
    assert (wake["status"], wake["reason"]) == ("done", "deferred")
    assert wake["event_id"].endswith(":done:deferred1")
    assert h.bridge.inflight("a")["delivery_id"] == "d2"


def test_deferred_blocked_always_wakes(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "idle")
    h.advance(21)
    h.controller("d1")
    h.status("w5:p1", "blocked")
    h.advance(5)
    assert len(h.sent) == 1
    h.finish("d1")
    h.advance(6)
    assert [(p["status"], p["reason"]) for p in h.sent[1:]] == [("blocked", "deferred")]


def test_deferred_dropped_when_pane_is_working_again(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "idle")
    h.advance(21)
    h.controller("d1")
    h.status("w5:p1", "working")
    h.advance(60)
    h.status("w5:p1", "done")
    h.advance(25)
    h.status("w5:p1", "working")
    h.finish("d1")
    h.advance(10)
    assert len(h.sent) == 1
    assert h.pane()["deferred"] is None


def test_author_and_reviewer_are_serialized_per_workflow(tmp_path):
    author = reg_task("author", "w5:pX", reviewer_pane_id="w5:pY")
    reviewer = reg_task("reviewer", "w5:pY", parent_task_id="author", parent_pane_id="w5:pX")
    h = Harness(tmp_path, tasks=[author, reviewer])
    assert h.bridge.workflow_key("reviewer") == "author"
    assert "parent_task_id" not in h.bridge.tasks["reviewer"]  # not in the wire task

    h.status("w5:pX", "idle")
    h.advance(21)
    assert [p["task"]["id"] for p in h.sent] == ["author"]
    h.controller("d1")
    # The controller hands work to the reviewer, and the author also works.
    h.status("w5:pY", "working")
    h.advance(40)
    h.status("w5:pY", "done")
    h.status("w5:pX", "working")
    h.advance(40)
    h.status("w5:pX", "done")
    h.advance(25)
    assert len(h.sent) == 1
    assert h.pane("reviewer")["deferred"] and h.pane("author")["deferred"]

    h.finish("d1")
    h.advance(6)
    assert [p["task"]["id"] for p in h.sent] == ["author", "reviewer"]
    assert h.sent[1]["reason"] == "deferred"
    assert h.bridge.inflight("author")["delivery_id"] == "d2"
    h.controller("d2")
    h.advance(60)
    assert len(h.sent) == 2  # author's deferred wake waits for the reviewer run
    assert set(h.bridge.state["workflows"]) == {"author"}

    h.finish("d2")
    h.advance(6)
    assert [(p["task"]["id"], p["reason"]) for p in h.sent] == [
        ("author", "status"), ("reviewer", "deferred"), ("author", "deferred")]


def test_inflight_assumed_ended_without_controller_row(tmp_path, capsys):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "idle")
    h.advance(21)
    h.advance(170)
    assert h.bridge.inflight("a") is not None
    h.advance(15)
    assert h.bridge.inflight("a") is None
    assert "how=assumed_no_row" in capsys.readouterr().out


def test_inflight_follows_open_row_until_hard_cap(tmp_path, capsys):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "idle")
    h.advance(21)
    h.controller("d1")
    h.advance(400, step=5)
    inflight = h.bridge.inflight("a")
    assert inflight is not None and inflight["controller_started_at"] is not None
    h.advance(1400, step=5)
    assert h.bridge.inflight("a") is None
    out = capsys.readouterr().out
    assert "WARNING controller still open" in out and "how=hard_cap" in out


def test_inflight_time_fallback_when_db_unreadable(tmp_path, capsys):
    h = Harness(tmp_path, tasks=[no_continue()], state_db=tmp_path / "missing" / "state.db")
    h.status("w5:p1", "idle")
    h.advance(21)
    h.advance(150)
    assert h.bridge.inflight("a") is not None
    h.advance(35)
    assert h.bridge.inflight("a") is None
    assert "how=assumed_db_unreadable" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------


def test_watchdog_stall_once_per_epoch_plus_one_repeat(tmp_path):
    h = Harness(tmp_path, auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(880, step=5)
    assert h.reasons() == ["status"]
    h.advance(90, step=5)
    assert h.reasons() == ["status", "stall"]
    assert h.sent[1]["event_id"].endswith(":idle:stall1")
    h.advance(1700, step=5)  # < 3x stall
    assert h.reasons() == ["status", "stall"]
    h.advance(100, step=5)  # >= 2700 s idle
    assert h.reasons() == ["status", "stall", "stall"]
    h.advance(8000, step=10)
    assert h.reasons() == ["status", "stall", "stall"]  # never more
    # New real work opens a new epoch.
    h.status("w5:p1", "working")
    h.advance(60, step=5)
    h.status("w5:p1", "idle")
    h.advance(970, step=5)
    assert h.reasons() == ["status", "stall", "stall", "status", "stall"]


def test_controller_clear_blip_does_not_reset_stall_budget(tmp_path):
    h = Harness(tmp_path, auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(3000, step=5)
    assert h.reasons().count("stall") == 2
    # A 2 s /clear is not real work: budget stays exhausted.
    h.status("w5:p1", "working")
    h.advance(2)
    h.status("w5:p1", "idle")
    h.advance(5000, step=10)
    assert h.reasons().count("stall") == 2


def test_wait_until_elapsed_fires_once_then_stall_clock_restarts(tmp_path):
    until = bridge.iso_time(T0 + 1200)
    h = Harness(tmp_path, tasks=[reg_task("a", "w5:p1", wait={"reason": "scheduled", "until": until,
                                                               "note": "check CI"})],
                auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(1150, step=5)
    assert h.reasons() == ["status"]  # until in the future: no stall at 900 s
    h.advance(100, step=5)
    assert h.reasons() == ["status", "wait_elapsed"]
    assert h.sent[1]["wait"] == {"reason": "scheduled", "until": until}
    h.advance(800, step=5)
    assert h.reasons() == ["status", "wait_elapsed"]
    h.advance(200, step=5)  # until + 900 s
    assert h.reasons() == ["status", "wait_elapsed", "stall"]


def test_user_decision_wait_suppresses_stall(tmp_path):
    h = Harness(tmp_path, tasks=[reg_task("a", "w5:p1", wait={"reason": "user_decision"})],
                auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(6000, step=10)
    assert h.reasons() == ["status"]
    assert h.sent[0]["wait"] == {"reason": "user_decision", "until": ""}


def test_active_wait_holds_short_progress_turns_but_not_long_ones(tmp_path):
    until = bridge.iso_time(T0 + 3600)
    h = Harness(tmp_path, tasks=[reg_task("a", "w5:p1", wait={"reason": "scheduled", "until": until, "quiet": True})],
                auto_end=True)
    # The harness pane starts as a snapshot `working` of unknown age: that one still wakes.
    h.status("w5:p1", "idle")
    h.advance(60, step=5)
    assert h.reasons() == ["status"]
    # A 10 s progress turn during the wait: no controller run.
    h.status("w5:p1", "working")
    h.advance(10, step=5)
    h.status("w5:p1", "done")
    h.advance(60, step=5)
    assert h.reasons() == ["status"]
    # A real 2-minute turn still wakes the controller.
    h.status("w5:p1", "working")
    h.advance(120, step=5)
    h.status("w5:p1", "done")
    h.advance(60, step=5)
    assert h.reasons() == ["status", "status"]
    # blocked is never held.
    h.status("w5:p1", "working")
    h.advance(5, step=5)
    h.status("w5:p1", "blocked")
    h.advance(30, step=5)
    assert h.reasons() == ["status", "status", "status"]


def test_plain_wait_does_not_hold_short_result_turns(tmp_path):
    # A short turn under a non-quiet wait can be a real result (a background suite just finished).
    until = bridge.iso_time(T0 + 3600)
    h = Harness(tmp_path, tasks=[reg_task("a", "w5:p1", wait={"reason": "scheduled", "until": until})],
                auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(60, step=5)
    assert h.reasons() == ["status"]
    h.status("w5:p1", "working")
    h.advance(10, step=5)
    h.status("w5:p1", "done")
    h.advance(60, step=5)
    assert h.reasons() == ["status", "status"]


def test_elapsed_wait_does_not_hold_short_turns(tmp_path):
    until = bridge.iso_time(T0 - 10)
    h = Harness(tmp_path, tasks=[reg_task("a", "w5:p1", wait={"reason": "scheduled", "until": until, "quiet": True})],
                auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(60, step=5)
    h.status("w5:p1", "working")
    h.advance(10, step=5)
    h.status("w5:p1", "done")
    h.advance(60, step=5)
    assert "status" in h.reasons()


def test_external_wait_without_until_suppresses_stall(tmp_path):
    # A reviewer/author waiting on the linked side is woken by that side's events, not the watchdog.
    h = Harness(tmp_path, tasks=[reg_task("a", "w5:p1", wait={"reason": "external"})],
                auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(6000, step=10)
    assert h.reasons() == ["status"]


def test_watchdog_skips_tasks_without_allow_continue(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()], auto_end=True)
    h.status("w5:p1", "idle")
    h.advance(4000, step=10)
    assert h.reasons() == ["status"]


def test_working_no_output_once_per_working_epoch(tmp_path):
    h = Harness(tmp_path, auto_end=True)
    h.revision_changes = False
    h.advance(1150, step=5)
    assert h.sent == []
    h.advance(400, step=5)
    assert [(p["status"], p["reason"]) for p in h.sent] == [("working", "working_no_output")]
    assert h.sent[0]["event_id"].endswith(":working:working_no_output1")
    h.advance(4000, step=10)
    assert len(h.sent) == 1


def test_working_with_changing_revision_never_fires(tmp_path):
    h = Harness(tmp_path, auto_end=True)
    h.advance(5000, step=10)
    assert h.sent == []


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


def test_ignored_rebuilds_once_then_final(tmp_path, capsys):
    h = Harness(tmp_path, tasks=[no_continue()])

    def ignored_after_registry_edit(payload):
        h.tasks[0]["phase"] = "edited-by-controller"
        h.write_registry()
        return bridge.DeliveryResult("ignored", "", "status=ignored")

    h.responses = [ignored_after_registry_edit, bridge.DeliveryResult("ignored", "", "status=ignored")]
    h.status("w5:p1", "idle")
    h.advance(21)
    h.advance(100)
    assert len(h.sent) == 2
    assert h.sent[0]["task"]["phase"] == "apply"
    assert h.sent[1]["task"]["phase"] == "edited-by-controller"
    assert h.sent[1]["task_fingerprint"] == bridge.task_fingerprint(h.bridge.tasks["a"])
    assert h.bridge.state["queue"] == []
    out = capsys.readouterr().out
    assert "reason=ignored_fingerprint_changed" in out and "reason=ignored " in out


def test_ignored_without_registry_change_is_dropped_once(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.responses = [bridge.DeliveryResult("ignored", "", "status=ignored")]
    h.status("w5:p1", "idle")
    h.advance(200)
    assert len(h.sent) == 1
    assert h.bridge.inflight("a") is None


def test_transport_failures_retry_with_bounded_backoff(tmp_path, capsys):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.responses = [bridge.DeliveryResult("error", "", "Error: connection refused")] * 10
    h.status("w5:p1", "idle")
    h.advance(1000)
    gaps = [round(b - a) for a, b in zip(h.sent_at, h.sent_at[1:])]
    assert gaps == [5, 15, 45, 120, 300]
    assert h.bridge.state["queue"] == []
    out = capsys.readouterr().out
    assert "ERROR dropped task=a" in out and "reason=transport attempts=6" in out


def test_other_tasks_are_delivered_while_one_is_backing_off(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue("a", "w5:p1"), no_continue("b", "w5:p2")])
    h.responses = [bridge.DeliveryResult("error", "", "timeout")] * 3
    h.status("w5:p1", "idle")
    h.advance(21)
    h.status("w5:p2", "idle")
    h.advance(29)  # a failed at +20, +25, +40 and now waits until +85
    assert [p["task"]["id"] for p in h.sent] == ["a", "a", "a", "b"]
    assert h.bridge.inflight("b") is not None
    assert [e["task_id"] for e in h.bridge.state["queue"]] == ["a"]


def test_payload_is_rebuilt_from_current_task_after_registry_edit(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "idle")
    h.advance(10)
    h.tasks[0]["phase"] = "review"
    h.write_registry()
    h.advance(11)
    assert len(h.sent) == 1
    assert h.sent[0]["task"]["phase"] == "review"
    assert h.sent[0]["task_fingerprint"] == bridge.task_fingerprint(h.bridge.tasks["a"])


def test_payload_contract_has_every_key_and_no_nulls(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    secret = "API Error: Connection error. SECRET-TEXT-sk-123"
    h.pane_text["w5:p1"] = "some output\n" + secret + "\n"
    h.status("w5:p1", "idle")
    h.advance(21)
    payload = h.sent[0]
    assert set(payload) == {"event_type", "event_id", "observed_at", "status", "reason",
                            "status_age_seconds", "coalesced", "hint", "wait", "task",
                            "task_fingerprint", "evidence", "telemetry"}
    assert set(payload["telemetry"]) == {"phase", "phase_started_at", "last_status",
                                         "last_status_duration_seconds", "phase_durations"}
    assert payload["hint"] == {"kind": "connection_error", "label": "Connection error"}
    assert payload["wait"] == {"reason": "none", "until": ""}
    assert isinstance(payload["status_age_seconds"], int) and payload["status_age_seconds"] >= 20
    assert isinstance(payload["coalesced"], int)
    assert "SECRET-TEXT" not in json.dumps(payload)

    def walk(value, path="payload"):
        assert value is not None, path
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
    walk(payload)


def test_legacy_transition_payload_also_meets_contract():
    task = {"id": "t", "pane_id": "w5:p1", "phase": "apply", "cwd": CWD}
    payload = bridge.transition({"panes": {}}, task, "idle", now="2026-09-24T00:00:00+00:00")
    assert payload["reason"] == "status" and payload["hint"] == {"kind": "none", "label": ""}
    assert payload["telemetry"]["last_status"] == ""
    assert payload["telemetry"]["last_status_duration_seconds"] == 0.0


@pytest.mark.parametrize("text,kind,label", [
    ("⎿  API Error: Connection error.", "connection_error", "Connection error"),
    ("Connection lost. Retrying in 30s", "connection_error", "Connection lost"),
    ("fetch failed (ECONNRESET)", "connection_error", "ECONNRESET"),
    ("  ⎿  API Error (Request timed out.)", "connection_error", "API Error"),
    ("Error: 529 overloaded_error", "connection_error", "overloaded"),
    ("request failed: socket hang up", "connection_error", "socket hang up"),
    ("HTTP 502 Bad Gateway", "connection_error", "HTTP 5xx"),
    ("Please run /login · API Error: 401 authentication_error", "login_required", "/login"),
    ("OAuth token has expired. Please obtain a new token", "login_required", "OAuth token expired"),
    ("Invalid API key · Fix external API key", "login_required", "Invalid API key"),
    ("API Error: 429 rate_limit_error", "rate_limit", "rate limit"),
    ("Claude usage limit reached. Your limit will reset at 5pm", "rate_limit", "usage limit"),
    ("❯ 1. Yes\n  2. No", "menu_open", "numbered selector"),
    ("Do you want to proceed?\n❯ 1. Yes", "menu_open", "numbered selector"),
    ("Enter to select · Esc to cancel", "menu_open", "Enter to select"),
    ("API Error: 400 Prompt is too long", "context_limit", "Prompt is too long"),
    ("❯ Ревью PASS, деплой и проверка\n⏵⏵ bypass permissions on", "none", ""),
    ("", "none", ""),
])
def test_hint_classification(text, kind, label):
    assert bridge.classify_hint(text) == {"kind": kind, "label": label}


def test_hint_prefers_recent_lines():
    old = "API Error: Connection error."
    text = "\n".join([old] + [f"line {i}" for i in range(20)] + ["Do you want to proceed?"])
    assert bridge.classify_hint(text)["kind"] == "menu_open"
    only_old = "\n".join([old] + [f"line {i}" for i in range(20)])
    assert bridge.classify_hint(only_old)["kind"] == "connection_error"


# --------------------------------------------------------------------------
# Registry, topology, state
# --------------------------------------------------------------------------


def test_bad_registry_json_keeps_last_good_tasks(tmp_path, capsys):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.write_registry("{ not json")
    h.advance(1)
    assert set(h.bridge.tasks) == {"a"}
    assert "registry load failed; keeping last good" in capsys.readouterr().out
    h.status("w5:p1", "idle")
    h.advance(21)
    assert len(h.sent) == 1
    h.write_registry(json.dumps({"version": 1, "tasks": [{"id": "bad id!", "pane_id": "x", "cwd": CWD}]}))
    h.advance(1)
    assert set(h.bridge.tasks) == {"a"}
    h.tasks[0]["phase"] = "fixed"
    h.write_registry()
    h.advance(1)
    assert h.bridge.tasks["a"]["phase"] == "fixed"


def test_resubscribe_only_when_pane_set_changes(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.tasks[0]["phase"] = "edited"
    h.write_registry()
    h.advance(1)
    assert h.bridge.tasks["a"]["phase"] == "edited"
    assert not h.bridge.need_resubscribe
    h.panes.append({"pane_id": "w5:p2", "cwd": CWD, "agent_status": "idle"})
    h.tasks.append(no_continue("b", "w5:p2"))
    h.write_registry()
    h.bridge.handle_message({"event": "pane.created", "data": {"pane_id": "w5:p2"}}, h.now)
    h.advance(6)
    assert h.bridge.need_resubscribe
    assert h.pane("b")["last_seen"] == "idle"  # baseline from the pane snapshot, no wake
    assert h.pane("b").get("wake") is None


def test_topology_refresh_is_rate_limited(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    calls = h.list_calls
    for _ in range(10):
        h.bridge.handle_message({"event": "pane.updated", "data": {}}, h.now)
        h.advance(0.5, step=0.5)
    assert h.list_calls - calls <= 1


def test_tombstoned_pane_is_not_resurrected_as_auto(tmp_path):
    stub = {"id": "old", "enabled": False, "completed": True, "tombstone": True,
            "session": "default", "workspace_id": "w5", "pane_id": "w5:p7", "cwd": CWD,
            "project": "ExampleApp", "change": "c"}
    panes = [{"pane_id": "w5:p7", "cwd": CWD, "agent_status": "idle"},
             {"pane_id": "w5:p1", "cwd": CWD, "agent_status": "working"}]
    h = Harness(tmp_path, tasks=[no_continue(), stub], panes=panes)
    assert set(h.bridge.tasks) == {"a"}


def test_legacy_state_file_is_tolerated_and_pending_migrated(tmp_path):
    task = no_continue()
    legacy_payload = {"event_type": "herdr.agent_status_changed", "event_id": "a:5:idle",
                      "status": "idle", "task": {"id": "a", "pane_id": "w5:p1"},
                      "task_fingerprint": "stale"}
    old_state = {"version": 1, "pending": [legacy_payload], "panes": {
        "a": {"last_seen": "idle", "generation": 5, "phase": "apply",
              "phase_started_at": "2026-09-20T00:00:00+00:00",
              "status_started_at": "2026-09-20T00:00:00+00:00",
              "last_status_duration_seconds": None},
        "auto:w8:p1": {"last_seen": "unknown", "generation": 10}}}
    panes = [{"pane_id": "w5:p1", "cwd": CWD, "agent_status": "idle"}]
    h = Harness(tmp_path, tasks=[task], panes=panes, state=old_state)
    assert h.bridge.state["pending"] == []
    assert h.pane()["status_since"] == T0
    h.advance(1)
    assert [p["event_id"] for p in h.sent] == ["a:5:idle"]
    assert h.sent[0]["task_fingerprint"] == bridge.task_fingerprint(h.bridge.tasks["a"])
    saved = json.loads(h.state_path.read_text())
    assert saved["version"] == 1 and saved["pending"] == []


def test_corrupt_state_file_starts_fresh(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()], state="{ broken")
    assert h.state_path.with_name(h.state_path.name + ".corrupt").exists()
    h.status("w5:p1", "idle")
    h.advance(21)
    assert len(h.sent) == 1


def test_snapshot_reconcile_catches_missed_transition(tmp_path):
    old_state = {"version": 1, "pending": [], "panes": {"a": {"last_seen": "working", "generation": 3}}}
    panes = [{"pane_id": "w5:p1", "cwd": CWD, "agent_status": "done"}]
    h = Harness(tmp_path, tasks=[no_continue()], panes=panes, state=old_state)
    h.advance(21)
    assert [(p["status"], p["event_id"]) for p in h.sent] == [("done", "a:4:done")]


def test_state_file_is_persisted_atomically_with_v2_keys(tmp_path):
    h = Harness(tmp_path, tasks=[no_continue()])
    h.status("w5:p1", "idle")
    h.advance(21)
    saved = json.loads(h.state_path.read_text())
    assert saved["version"] == 1
    pane = saved["panes"]["a"]
    for key in ("last_seen", "status_since", "generation", "working_epoch",
                "last_work_started", "last_work_ended", "last_delivery"):
        assert key in pane, key
    assert saved["workflows"]["a"]["inflight"]["delivery_id"] == "d1"


def test_threaded_delivery_never_blocks_the_loop(tmp_path):
    release = threading.Event()
    h = Harness(tmp_path, tasks=[no_continue("a", "w5:p1"), no_continue("b", "w5:p2")], threaded=True)

    def slow(payload):
        release.wait(10)
        return bridge.DeliveryResult(ACCEPTED, "slow-1", "status=accepted")

    h.responses = [slow]
    h.status("w5:p1", "idle")
    started = time.monotonic()
    h.advance(21)
    assert time.monotonic() - started < 2.0  # ticks returned while delivery hangs
    assert h.bridge.sending is not None
    h.status("w5:p2", "blocked")  # socket events are still processed
    h.advance(3)
    assert h.pane("b")["last_seen"] == "blocked"
    release.set()
    deadline = time.monotonic() + 5
    while not h.bridge.sending["job"].done() and time.monotonic() < deadline:
        time.sleep(0.01)
    h.advance(1)
    assert h.bridge.inflight("a")["delivery_id"] == "slow-1"
    h.advance(1)
    while h.bridge.sending is not None and time.monotonic() < deadline:
        time.sleep(0.01)
        h.advance(1)
    assert [p["task"]["id"] for p in h.sent] == ["a", "b"]


def test_stuck_worker_is_abandoned_as_transport_failure(tmp_path, capsys):
    release = threading.Event()
    h = Harness(tmp_path, tasks=[no_continue()], threaded=True)
    h.responses = [lambda payload: (release.wait(10), bridge.DeliveryResult("error", "", "late"))[1]]
    try:
        h.status("w5:p1", "idle")
        h.advance(21)
        assert h.bridge.sending is not None
        h.advance(90)  # > 45 s + 30 s grace, then the 5 s retry backoff
        deadline = time.monotonic() + 5
        while h.bridge.inflight("a") is None and time.monotonic() < deadline:
            time.sleep(0.01)
            h.advance(1)
        assert len(h.sent) == 2
        assert h.bridge.inflight("a")["delivery_id"] == "d1"
        assert "reason=transport attempt=1" in capsys.readouterr().out
    finally:
        release.set()


def test_run_loop_end_to_end_with_fake_socket_and_binaries(tmp_path):
    """Drive the real ``run()`` socket loop in a subprocess (no live Herdr/Hermes)."""
    import socket
    import subprocess

    sock_path = tmp_path / "h.sock"
    if len(str(sock_path)) > 100:
        pytest.skip("temporary path too long for AF_UNIX")
    registry = tmp_path / "state" / "herdr_tasks.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"version": 1, "tasks": [no_continue()]}))
    sent_file = tmp_path / "sent.jsonl"
    herdr = tmp_path / "herdr"
    herdr.write_text(
        "#!/usr/bin/env python3\nimport json, sys\nargs = sys.argv[3:]\n"
        "if args[:2] == ['pane', 'list']:\n"
        f"    print(json.dumps({{'result': {{'panes': [{{'pane_id': 'w5:p1', 'cwd': {CWD!r}, "
        "'agent_status': 'working'}]}}))\n"
        "elif args[:2] == ['pane', 'read']:\n    print('Do you want to proceed?')\n"
        "else:\n    print(json.dumps({'result': {'agent': {'revision': 1}}}))\n")
    hermes = tmp_path / "hermes"
    hermes.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        f"open({str(sent_file)!r}, 'a').write(sys.argv[sys.argv.index('--payload') + 1] + '\\n')\n"
        "print('Response (202): {\"status\": \"accepted\", \"delivery_id\": \"77\"}')\n")
    herdr.chmod(0o700)
    hermes.chmod(0o700)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    requests, stop = [], threading.Event()

    def serve():
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\n" not in data:
                data += conn.recv(65536)
            requests.append(json.loads(data.split(b"\n")[0]))
            conn.sendall(b'{"id":"x","result":{"type":"subscription_started"}}\n')
            conn.sendall(b'not json\n')
            conn.sendall(json.dumps({"event": "pane.agent_status_changed",
                                     "data": {"pane_id": "w5:p1", "agent_status": "idle"}}).encode() + b"\n")
            stop.wait(15)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS / "herdr_event_bridge.py"), "--registry", str(registry),
         "--state", str(tmp_path / "state" / "bridge.json"), "--socket", str(sock_path),
         "--hermes-bin", str(hermes), "--herdr-bin", str(herdr), "--project", "ExampleApp",
         "--cwd-prefix", CWD, "--debounce-seconds", "1", "--state-db", str(tmp_path / "none.db")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={"PATH": "/usr/bin:/bin", "HERMES_HOME": str(tmp_path)})
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not sent_file.exists():
            time.sleep(0.1)
        time.sleep(1.5)  # let the loop record the result
    finally:
        stop.set()
        proc.terminate()
        output = proc.communicate(timeout=10)[0]
        server.close()
    assert sent_file.exists(), output
    payload = json.loads(sent_file.read_text().splitlines()[0])
    assert (payload["status"], payload["reason"]) == ("idle", "status")
    assert payload["hint"] == {"kind": "menu_open", "label": "Do you want to proceed"}
    assert {"type": "pane.agent_status_changed", "pane_id": "w5:p1"} in requests[0]["params"]["subscriptions"]
    state = json.loads((tmp_path / "state" / "bridge.json").read_text())
    assert state["workflows"]["a"]["inflight"]["delivery_id"] == "77"
    assert "ignored malformed Herdr event" in output and "delivered task=a" in output


# --------------------------------------------------------------------------
# End to end with the real route script behind a local CLI fixture
# --------------------------------------------------------------------------


def route_cli(tmp_path, home):
    (Path(home) / bridge.PIPELINE_CONFIG).write_text(json.dumps({
        "profile": "exampleapp", "project": "ExampleApp", "cwd_prefixes": ["/srv/example-app"],
        "sibling_prefix": False, "route": "herdr-agent-events"}))
    script = tmp_path / "hermes-fixture"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import contextlib, io, json, pathlib, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import herdr_workflow_context as route\n"
        f"route.HOME = pathlib.Path({str(home)!r})\n"
        "sys.stdin = io.StringIO(sys.argv[sys.argv.index('--payload') + 1])\n"
        "out = io.StringIO()\n"
        "with contextlib.redirect_stdout(out): route.main()\n"
        "text = out.getvalue()\n"
        f"pathlib.Path({str(tmp_path / 'routed.jsonl')!r}).open('a').write(text or 'IGNORED\\n')\n"
        "reply = {'status': 'accepted', 'delivery_id': '42'} if text else {'status': 'ignored'}\n"
        "print('Response (202): ' + json.dumps(reply))\n"
    )
    script.chmod(0o700)
    return str(script)


def test_bridge_payload_passes_real_route_script(tmp_path):
    author = reg_task("author", "w5:pX", reviewer_pane_id="w5:pY", brief="x" * 3000,
                      notes=[{"at": "t", "by": "c", "text": str(i)} for i in range(6)],
                      decisions=[{"at": "t", "source": "u", "text": str(i)} for i in range(8)],
                      controller_disposition="long prose " * 50,
                      wait={"reason": "external", "until": ""})
    reviewer = reg_task("reviewer", "w5:pY", parent_task_id="author", parent_pane_id="w5:pX")
    h = Harness(tmp_path, tasks=[author, reviewer])
    cli = route_cli(tmp_path, tmp_path)
    h.bridge._deliver = lambda payload: bridge.deliver_payload(cli, "fixture-only", payload, 10)
    h.status("w5:pY", "done")
    h.advance(21)
    routed = json.loads((tmp_path / "routed.jsonl").read_text().splitlines()[0])
    for key in ("reason", "hint", "wait", "coalesced", "status_age_seconds"):
        assert key in routed, key
    assert routed["workflow"]["source_role"] == "reviewer"
    projected = routed["workflow"]["author"]
    assert len(projected["brief"]) <= 1501
    assert [n["text"] for n in projected["notes"]] == ["3", "4", "5"]
    assert [d["text"] for d in projected["decisions"]] == ["3", "4", "5", "6", "7"]
    assert projected["wait"] == {"reason": "external", "until": ""}
    assert "controller_disposition" not in projected
    assert h.bridge.inflight("author")["delivery_id"] == "42"
    h.controller("42", ended=h.now)
    h.advance(6)
    assert h.bridge.inflight("author") is None
    # ``completed`` is not part of the wire task, so only the route rejects
    # it: ``ignored`` with an unchanged fingerprint is a final drop.
    h.tasks[1]["completed"] = True
    h.write_registry()
    h.status("w5:pY", "working")
    h.advance(30)
    h.status("w5:pY", "idle")
    h.advance(40)
    assert (tmp_path / "routed.jsonl").read_text().splitlines()[-1] == "IGNORED"
    assert len((tmp_path / "routed.jsonl").read_text().splitlines()) == 2
    assert h.bridge.state["queue"] == [] and h.bridge.inflight("author") is None
