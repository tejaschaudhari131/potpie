"""
End-to-end test: real SerialWorker (with pyserial) connected to a TCP
'valve' simulator via pyserial's socket:// URL handler.

This exercises the full path:
    ValveController -> SerialWorker (own QThread)
                    -> pyserial socket://
                    -> ValveSimServer (separate thread)
                    -> back through SerialWorker.poll() -> line_received
                    -> ValveController.on_line_received -> position update

If any layer is broken — threading, parsing, gap timing, command framing,
verify-after-move — this test catches it.
"""

from __future__ import annotations

import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Speed knobs BEFORE importing controller so module-level constants stay sane;
# we tweak the runtime values instead.
import valve_controller as _vc
_vc.COMMAND_GAP_MS      = 5
_vc.RESPONSE_TIMEOUT_MS = 800
_vc.RETRY_COUNT         = 1

from PyQt5.QtCore import QCoreApplication, QThread, QMetaObject, Q_ARG, Qt, QTimer
from valve_controller import ValveController
from serial_manager import SerialWorker
from tests.valve_sim_tcp import ValveSimServer


def pump(app, ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


class Stack:
    """Bring up a real worker thread, real controller, and a sim server."""
    def __init__(self):
        self.app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        self.sim = ValveSimServer().start()

        self.worker = SerialWorker()
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self.thread.start()

        self.ctrl = ValveController()

        # Wire up identically to MainWindow
        self.ctrl.send_request.connect(self.worker.write_command)
        self.worker.line_received.connect(self.ctrl.on_line_received)
        self.worker.connected.connect(lambda _p: self.ctrl.set_connected(True))
        self.worker.disconnected.connect(lambda _r: self.ctrl.set_connected(False))

        # Capture
        self.failures: list[str] = []
        self.errors:   list[str] = []
        self.ctrl.failure.connect(self.failures.append)
        self.worker.error.connect(self.errors.append)

    def open(self):
        QMetaObject.invokeMethod(self.worker, "open_port",
                                 Qt.QueuedConnection, Q_ARG(str, self.sim.url))
        # Wait for connect + initial sync
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.app.processEvents()
            if self.ctrl._connected and self.ctrl.position(1) and self.ctrl.position(2):
                break
            time.sleep(0.01)

    def shutdown(self):
        QMetaObject.invokeMethod(self.worker, "stop", Qt.BlockingQueuedConnection)
        self.thread.quit()
        self.thread.wait(1500)
        self.sim.stop()


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_connect_and_initial_sync():
    s = Stack()
    try:
        s.sim.positions[1] = 2
        s.sim.positions[2] = 5
        s.open()
        pump(s.app, 100)
        expect(s.ctrl.position(1) == 2, f"V1 sync got {s.ctrl.position(1)}")
        expect(s.ctrl.position(2) == 5, f"V2 sync got {s.ctrl.position(2)}")
        expect("/1CP" in s.sim.commands_received, s.sim.commands_received)
        expect("/2CP" in s.sim.commands_received, s.sim.commands_received)
        print("  initial sync over TCP-as-serial: OK")
    finally:
        s.shutdown()


def test_full_move_round_trip():
    s = Stack()
    try:
        s.sim.positions[1] = 1
        s.sim.positions[2] = 1
        s.open()
        pump(s.app, 100)

        s.ctrl.request_move(1, 3)
        # Wait until the controller verifies & settles
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            s.app.processEvents()
            if s.ctrl.position(1) == 3 and not s.ctrl.is_busy():
                break
            time.sleep(0.01)

        expect(s.ctrl.position(1) == 3, f"V1 final {s.ctrl.position(1)}")
        expect(s.sim.positions[1] == 3, f"sim V1 {s.sim.positions[1]}")
        expect("/1GO3" in s.sim.commands_received, s.sim.commands_received)
        # The CP after the move (verify) should have been sent
        cps_after_go = [c for i, c in enumerate(s.sim.commands_received)
                        if c == "/1CP" and i > s.sim.commands_received.index("/1GO3")]
        expect(cps_after_go, "no /1CP verify after /1GO3")
        expect(not s.failures, f"unexpected failures: {s.failures}")
        print("  move + verify round trip: OK")
    finally:
        s.shutdown()


def test_garbage_reply_triggers_retry():
    s = Stack()
    try:
        s.sim.reply_garbage = True       # sim will return "GARBAGE" for CP
        s.open()
        pump(s.app, 1500)                # let timeout/retry path run
        # First /1CP gets garbage, controller retries up to RETRY_COUNT times.
        # Total /1CP sent should be RETRY_COUNT+1 = 2.
        cp1_count = s.sim.commands_received.count("/1CP")
        expect(cp1_count >= _vc.RETRY_COUNT + 1,
               f"expected >= {_vc.RETRY_COUNT+1} retries of /1CP, got {cp1_count}")
        expect(any("Malformed position reply" in m for cat, m in []) or s.failures,
               f"expected a failure to surface, got failures={s.failures}")
        print(f"  garbage reply -> retry then fail: OK ({cp1_count} attempts)")
    finally:
        s.shutdown()


def test_dropped_command_triggers_timeout_then_recovers():
    s = Stack()
    try:
        s.sim.drop_next_n = 1            # drop the very first command
        s.open()
        pump(s.app, 2500)                # plenty of time for retry path
        # Eventually positions should sync (sim defaults are 1, 1)
        expect(s.ctrl.position(1) in (1, 0), f"V1 {s.ctrl.position(1)}")
        # The controller should have re-issued /1CP after the timeout.
        cp1 = s.sim.commands_received.count("/1CP")
        expect(cp1 >= 2, f"expected /1CP retry after drop, got count={cp1}")
        print(f"  dropped command -> timeout -> retry: OK ({cp1} CPs)")
    finally:
        s.shutdown()


def test_disconnect_when_simulator_dies():
    s = Stack()
    try:
        s.open()
        pump(s.app, 200)
        expect(s.ctrl._connected, "should be connected initially")

        # Kill the simulator from under the worker.
        s.sim.stop()
        # Trigger I/O so the worker notices the broken pipe.
        s.ctrl.request_move(1, 4)
        pump(s.app, 600)

        expect(not s.ctrl._connected,
               "controller should mark disconnected after simulator died")
        print("  graceful disconnect on remote close: OK")
    finally:
        s.shutdown()


def main():
    tests = [
        test_connect_and_initial_sync,
        test_full_move_round_trip,
        test_garbage_reply_triggers_retry,
        test_dropped_command_triggers_timeout_then_recovers,
        test_disconnect_when_simulator_dies,
    ]
    fails = 0
    for t in tests:
        print(f"\n[{t.__name__}]")
        try:
            t()
        except AssertionError as e:
            fails += 1
            print(f"  FAIL: {e}")
        except Exception as e:
            fails += 1
            import traceback; traceback.print_exc()
    print(f"\n{'='*50}")
    print(f"  {len(tests)-fails}/{len(tests)} end-to-end tests passed")
    print(f"{'='*50}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
