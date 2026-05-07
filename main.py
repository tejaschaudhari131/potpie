"""
Valve Communication Software — entry point.

Run with:
    python main.py

Architecture (high level):

    +------------------+        Qt signals        +-----------------+
    |   MainWindow     |  ──────────────────────▶ | ValveController |
    |  (GUI thread)    |  ◀──────────────────────  |  (state machine)|
    +--------┬---------+                          +--------┬--------+
             │ port clicks                                  │ send/receive
             ▼                                              ▼
    +------------------+      QThread boundary    +-----------------+
    | Valve widgets    |                          |  SerialWorker   |
    | (custom QWidget) |                          |  (own QThread)  |
    +------------------+                          +-----------------+
                                                           │
                                                           ▼
                                                       pyserial
                                                           │
                                                           ▼
                                                   physical valves
"""

from __future__ import annotations

import sys
import traceback

from PyQt5.QtWidgets import QApplication, QMessageBox

from gui.main_window import MainWindow


def _excepthook(exc_type, exc_value, exc_tb):
    """Last-resort handler so the app never dies silently in production."""
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(text, file=sys.stderr)
    try:
        QMessageBox.critical(
            None,
            "Unexpected error",
            f"An unexpected error occurred:\n\n{exc_value}\n\nSee console for details.",
        )
    except Exception:
        pass


def main() -> int:
    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    app.setApplicationName("Valve Communication Software")
    app.setOrganizationName("Industrial Controls")

    win = MainWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
