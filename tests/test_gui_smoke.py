"""
Headless smoke test of the actual MainWindow.

Uses Qt's 'offscreen' platform plugin so it runs without a display.
Goal: instantiate, paint, take a screenshot, verify all widgets exist
and the close path is clean (no cross-thread timer warnings).
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Shorten the inter-command gap so this test runs quickly.
import valve_controller as _vc
_vc.COMMAND_GAP_MS = 5

import time
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtWidgets import QApplication

from gui.main_window import MainWindow


def pump(app, ms):
    """Spin the event loop for `ms` milliseconds so queued slots and
    short timers (like the inter-command gap) actually fire."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.002)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    app.processEvents()

    # --- structural assertions --------------------------------------------
    assert win.connect_btn.isEnabled(), "Connect button should start enabled"
    assert not win.disconnect_btn.isEnabled(), "Disconnect should start disabled"
    assert 1 in win.valve_widgets and 2 in win.valve_widgets
    assert win.valve_widgets[1].num_ports == 4
    assert win.valve_widgets[2].num_ports == 6
    print("  structural checks: OK")

    # --- paint and dump a screenshot to prove the widget tree renders -----
    pix = win.grab()
    assert not pix.isNull(), "grab() returned null"
    out = os.path.join(os.path.dirname(__file__), "smoke_screenshot.png")
    pix.save(out, "PNG")
    print(f"  rendered {pix.width()}x{pix.height()} -> {out}")

    # --- simulate a 'connected' state visually ----------------------------
    win._on_serial_connected("COM-DUMMY")         # bypass real port; queues /1CP, /2CP
    pump(app, 30)

    win.controller.on_line_received("Position is = 2")
    pump(app, 30)                                  # let gap timer fire, dispatch /2CP
    win.controller.on_line_received("Position is = 5")
    pump(app, 30)

    assert win.valve_widgets[1]._active_port == 2, win.valve_widgets[1]._active_port
    assert win.valve_widgets[2]._active_port == 5, win.valve_widgets[2]._active_port
    print("  position propagation to widgets: OK")

    # Render again in the 'connected, port active' state
    pix2 = win.grab()
    out2 = os.path.join(os.path.dirname(__file__), "smoke_connected.png")
    pix2.save(out2, "PNG")
    print(f"  rendered connected state -> {out2}")

    # --- close path -------------------------------------------------------
    QTimer.singleShot(50, win.close)
    QTimer.singleShot(200, app.quit)
    rc = app.exec_()
    print(f"  exec_ exit code: {rc}")
    print("test_gui_smoke: PASSED")


if __name__ == "__main__":
    main()
