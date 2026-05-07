"""
Headless integration tests for ValveController.

We instantiate a real QCoreApplication, capture every command the
controller emits, and feed synthetic replies back in. No serial port,
no GUI — but the *exact* state machine the GUI relies on.
"""

from __future__ import annotations

import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import QCoreApplication, QTimer

import valve_controller as vc_mod
from valve_controller import ValveController


# Speed the test up: shorter timeouts and gaps.
vc_mod.RESPONSE_TIMEOUT_MS = 200
vc_mod.COMMAND_GAP_MS      = 5
vc_mod.RETRY_COUNT         = 2


# -------------------------- harness ---------------------------------------
class Harness:
    def __init__(self):
        self.app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        self.ctrl = ValveController()
        self.sent: list[str] = []
        self.failures: list[str] = []
        self.positions: list[tuple[int,int]] = []
        self.settled: list[tuple[int,int]] = []
        self.moving:  list[tuple[int,int]] = []
        self.busy_states: list[bool] = []
        self.logs: list[tuple[str,str]] = []

        self.ctrl.send_request.connect(self.sent.append)
        self.ctrl.failure.connect(self.failures.append)
        self.ctrl.position_changed.connect(lambda v,p: self.positions.append((v,p)))
        self.ctrl.valve_settled.connect(lambda v,p: self.settled.append((v,p)))
        self.ctrl.valve_moving.connect(lambda v,p: self.moving.append((v,p)))
        self.ctrl.busy_changed.connect(lambda b: self.busy_states.append(b))
        self.ctrl.log.connect(lambda c,m: self.logs.append((c,m)))

    def pump(self, ms=20):
        """Spin the event loop briefly so queued slots run."""
        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.002)

    def reply(self, text: str):
        self.ctrl.on_line_received(text)
        self.pump(20)

    def reset_logs(self):
        self.sent.clear(); self.failures.clear(); self.positions.clear()
        self.settled.clear(); self.moving.clear(); self.busy_states.clear()
        self.logs.clear()


# -------------------------- assertions ------------------------------------
def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


# -------------------------- tests -----------------------------------------
def test_startup_sync_queries_both_valves():
    h = Harness()
    h.ctrl.set_connected(True)
    h.pump(30)
    # Only the first query is in flight; the second is queued and will
    # be sent after the first reply lands.
    expect(h.sent == ["/1CP"], f"only /1CP should be in flight, got {h.sent}")

    h.reply("Position is = 1")          # valve 1 initial pos
    expect(h.sent == ["/1CP", "/2CP"], f"after V1 reply: {h.sent}")
    h.reply("Position is = 4")          # valve 2 initial pos
    expect(h.ctrl.position(1) == 1, f"V1 pos {h.ctrl.position(1)}")
    expect(h.ctrl.position(2) == 4, f"V2 pos {h.ctrl.position(2)}")
    expect((1,1) in h.positions and (2,4) in h.positions,
           f"position_changed missed: {h.positions}")
    print("  startup sync: OK")


def test_move_then_verify_sequence():
    h = Harness()
    h.ctrl.set_connected(True)
    h.pump(30)
    h.reply("Position is = 1")          # ack /1CP
    h.reply("Position is = 1")          # ack /2CP
    h.reset_logs()

    h.ctrl.request_move(1, 3)
    h.pump(30)
    expect(h.sent == ["/1GO3"], f"first cmd should be GO, got {h.sent}")
    expect(h.moving == [(1, 3)], f"valve_moving signal: {h.moving}")

    h.reply("OK")                       # ack of move → triggers verify
    expect(h.sent == ["/1GO3", "/1CP"], f"verify not enqueued: {h.sent}")

    h.reply("Position is = 3")          # verify reply
    expect(h.ctrl.position(1) == 3, f"final pos {h.ctrl.position(1)}")
    expect(h.settled == [(1, 3)], f"valve_settled: {h.settled}")
    expect(h.failures == [], f"unexpected failure: {h.failures}")
    print("  move+verify sequence: OK")


def test_position_mismatch_reports_failure():
    h = Harness()
    h.ctrl.set_connected(True)
    h.pump(30)
    h.reply("Position is = 1"); h.reply("Position is = 1")
    h.reset_logs()

    h.ctrl.request_move(2, 5)
    h.pump(30)
    expect(h.sent == ["/2GO5"], h.sent)

    h.reply("OK")
    h.reply("Position is = 2")          # wrong — hardware didn't reach 5

    expect(any("did not reach" in f for f in h.failures),
           f"expected mismatch failure, got {h.failures}")
    print("  position mismatch: OK")


def test_rapid_clicks_are_serialised_and_collapsed():
    h = Harness()
    h.ctrl.set_connected(True)
    h.pump(30)
    h.reply("Position is = 1"); h.reply("Position is = 1")
    h.reset_logs()

    # Fire 5 clicks faster than any reply can arrive.
    for p in (2, 3, 4, 4, 3):
        h.ctrl.request_move(1, p)
    h.pump(30)

    # Only the first command should have been *sent* — the rest are queued.
    expect(h.sent == ["/1GO2"], f"expected one TX in flight, got {h.sent}")

    # Walk through the queue: every move is followed by a CP verify.
    h.reply("OK"); h.reply("Position is = 2")   # 1→2
    h.reply("OK"); h.reply("Position is = 3")   # 1→3
    h.reply("OK"); h.reply("Position is = 4")   # 1→4
    # The duplicate (4) should have been collapsed; (3) at the end is unique.
    h.reply("OK"); h.reply("Position is = 3")   # 1→3

    expected_tx = [
        "/1GO2", "/1CP",
        "/1GO3", "/1CP",
        "/1GO4", "/1CP",
        "/1GO3", "/1CP",
    ]
    expect(h.sent == expected_tx, f"queue order wrong:\n got: {h.sent}\n exp: {expected_tx}")
    expect(h.ctrl.position(1) == 3, f"final pos {h.ctrl.position(1)}")
    print("  rapid-click queue + collapse: OK")


def test_timeout_retries_then_fails():
    h = Harness()
    h.ctrl.set_connected(True)
    h.pump(30)
    h.reply("Position is = 1"); h.reply("Position is = 1")
    h.reset_logs()

    h.ctrl.request_move(1, 2)
    h.pump(30)
    expect(h.sent == ["/1GO2"], h.sent)

    # Don't reply — wait long enough to cover RESPONSE_TIMEOUT_MS * (RETRY_COUNT+1).
    deadline = vc_mod.RESPONSE_TIMEOUT_MS * (vc_mod.RETRY_COUNT + 2) + 200
    h.pump(deadline)

    # Should have re-sent the GO command on every retry.
    expect(h.sent.count("/1GO2") == vc_mod.RETRY_COUNT + 1,
           f"retry count wrong: {h.sent}")
    expect(any("Communication timeout" in f for f in h.failures),
           f"expected timeout failure, got {h.failures}")
    print(f"  timeout+retry ({vc_mod.RETRY_COUNT} retries): OK")


def test_disconnect_clears_state():
    h = Harness()
    h.ctrl.set_connected(True)
    h.pump(30)
    h.reply("Position is = 2"); h.reply("Position is = 5")
    expect(h.ctrl.position(1) == 2 and h.ctrl.position(2) == 5, "preconditions")

    h.ctrl.set_connected(False)
    expect(h.ctrl.position(1) == 0 and h.ctrl.position(2) == 0,
           "positions should reset to unknown on disconnect")
    expect(not h.ctrl.is_busy(), "controller should not be busy after disconnect")

    # Requests while disconnected fail loudly, never silently.
    h.failures.clear(); h.sent.clear()
    h.ctrl.request_move(1, 3)
    h.pump(20)
    expect(h.sent == [], f"no command should have been sent: {h.sent}")
    expect(any("Not connected" in f for f in h.failures), h.failures)
    print("  disconnect cleanup: OK")


def test_invalid_port_rejected():
    h = Harness()
    h.ctrl.set_connected(True)
    h.pump(30); h.reply("Position is = 1"); h.reply("Position is = 1")
    h.reset_logs()

    h.ctrl.request_move(1, 5)            # valve-1 only has 4 ports
    h.ctrl.request_move(2, 7)            # valve-2 only has 6 ports
    h.ctrl.request_move(3, 1)            # no such valve
    h.pump(20)

    expect(h.sent == [], f"no commands should be queued: {h.sent}")
    expect(len(h.failures) == 3, f"expected 3 failures, got {h.failures}")
    print("  bad-input rejection: OK")


def test_malformed_reply_triggers_retry():
    h = Harness()
    h.ctrl.set_connected(True)
    h.pump(30)
    # First sync query — answer with garbage to force a retry of the *query*
    # (GO commands accept any ack, so we hit the parser via CP).
    h.reply("garbage and nonsense")

    # Should have retried by re-sending /1CP.
    expect(h.sent.count("/1CP") >= 2, f"expected retry of /1CP, got {h.sent}")
    print("  malformed reply retry: OK")


# -------------------------- run -------------------------------------------
def main():
    tests = [
        test_startup_sync_queries_both_valves,
        test_move_then_verify_sequence,
        test_position_mismatch_reports_failure,
        test_rapid_clicks_are_serialised_and_collapsed,
        test_timeout_retries_then_fails,
        test_disconnect_clears_state,
        test_invalid_port_rejected,
        test_malformed_reply_triggers_retry,
    ]
    failures = 0
    for t in tests:
        print(f"\n[{t.__name__}]")
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR: {type(e).__name__}: {e}")
    print(f"\n{'='*50}")
    print(f"  {len(tests)-failures}/{len(tests)} tests passed")
    print(f"{'='*50}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
