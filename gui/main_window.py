"""
Main window: connection bar, two valve diagrams and a serial monitor.

This module owns:
    * the SerialWorker (running on a dedicated QThread)
    * the ValveController (running on the GUI thread)
    * the visual widgets

It is responsible for translating controller signals into pixel updates
and translating user clicks into controller requests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict

from PyQt5.QtCore import Qt, QThread, QTimer, QMetaObject, Q_ARG, pyqtSlot
from PyQt5.QtGui import QFont, QColor, QPalette, QTextCursor
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QFrame, QPlainTextEdit, QStatusBar,
    QSizePolicy, QToolButton,
)

from config import VALVES, AUTO_RECONNECT_MS, LOG_MAX_LINES
from serial_manager import SerialWorker, available_ports
from valve_controller import ValveController
from gui.valve_widget import ValveWidget, Side


# ---------------------------------------------------------------------------
# Small status pill helper
# ---------------------------------------------------------------------------
class StatusPill(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("● Disconnected")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumWidth(150)
        self.set_state("disconnected")

    def set_state(self, state: str) -> None:
        styles = {
            "disconnected": ("#5a1f1f", "#ff6b6b", "● Disconnected"),
            "connected":    ("#16361f", "#3FD17A", "● Connected"),
            "busy":         ("#3a2f10", "#FFB020", "● Busy"),
            "error":        ("#5a1f1f", "#ff6b6b", "● Error"),
        }
        bg, fg, txt = styles.get(state, styles["disconnected"])
        self.setText(txt)
        self.setStyleSheet(
            f"QLabel {{ background:{bg}; color:{fg}; "
            f"border-radius:10px; padding:6px 14px; font-weight:bold; }}"
        )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Valve Communication Software")
        self.resize(1180, 720)
        self._apply_dark_theme()

        # ------------------------------------------------------------- model
        self.controller = ValveController()

        # Serial worker on its own thread.
        # The worker's start() slot creates its own poll timer with the
        # correct thread affinity — we just kick it off when the thread runs.
        self._thread = QThread(self)
        self.worker  = SerialWorker()
        self.worker.moveToThread(self._thread)
        self._thread.started.connect(self.worker.start)
        self._thread.start()

        # Auto-reconnect timer
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(AUTO_RECONNECT_MS)
        self._reconnect_timer.timeout.connect(self._try_auto_reconnect)
        self._auto_reconnect_target: str = ""

        # ------------------------------------------------------------- view
        self._build_ui()
        self._wire_signals()

        self._refresh_ports()
        self._set_connected_ui(False)

    # =====================================================================
    # UI construction
    # =====================================================================
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(12)

        root.addWidget(self._build_header())
        root.addWidget(self._build_connection_bar())
        root.addWidget(self._build_valve_panel(), stretch=1)
        root.addWidget(self._build_log_panel(), stretch=0)

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready")

    def _build_header(self) -> QWidget:
        w = QFrame()
        w.setObjectName("header")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)
        title = QLabel("Valve Communication Software")
        f = QFont(); f.setPointSize(16); f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)
        lay.addStretch(1)
        self.status_pill = StatusPill()
        lay.addWidget(self.status_pill)
        return w

    def _build_connection_bar(self) -> QWidget:
        w = QFrame()
        w.setObjectName("card")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        lay.addWidget(QLabel("COM Port"))

        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(160)
        lay.addWidget(self.port_combo)

        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("⟳")
        self.refresh_btn.setToolTip("Rescan available COM ports")
        self.refresh_btn.clicked.connect(self._refresh_ports)
        lay.addWidget(self.refresh_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        lay.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        lay.addWidget(self.disconnect_btn)

        lay.addSpacing(20)

        self.busy_label = QLabel("")
        self.busy_label.setStyleSheet("color:#FFB020; font-weight:bold;")
        lay.addWidget(self.busy_label)

        lay.addStretch(1)

        self.sync_btn = QPushButton("Sync Positions")
        self.sync_btn.setToolTip("Re-query the current position of both valves")
        self.sync_btn.clicked.connect(self.controller.sync_all_positions)
        lay.addWidget(self.sync_btn)

        return w

    def _build_valve_panel(self) -> QWidget:
        w = QFrame()
        w.setObjectName("card")
        grid = QGridLayout(w)
        grid.setContentsMargins(20, 18, 20, 18)
        grid.setHorizontalSpacing(40)

        # Valve-1 on the left, ports on its left side; Valve-2 on the right,
        # ports on its right side — matches the reference panel.
        spec1 = VALVES[1]
        spec2 = VALVES[2]
        self.valve_widgets: Dict[int, ValveWidget] = {
            1: ValveWidget(1, spec1.name, spec1.num_ports, spec1.color, side=Side.LEFT),
            2: ValveWidget(2, spec2.name, spec2.num_ports, spec2.color, side=Side.RIGHT),
        }
        for vw in self.valve_widgets.values():
            vw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            vw.port_clicked.connect(self.controller.request_move)

        # A short bus indicator between the two rotors
        bus = QLabel()
        bus.setFixedHeight(6)
        bus.setStyleSheet("background:#3b3f45; border-radius:3px;")
        bus.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        grid.addWidget(self.valve_widgets[1], 0, 0)
        grid.addWidget(bus,                   0, 1, alignment=Qt.AlignVCenter)
        grid.addWidget(self.valve_widgets[2], 0, 2)
        grid.setColumnStretch(0, 5)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 5)

        return w

    def _build_log_panel(self) -> QWidget:
        w = QFrame()
        w.setObjectName("card")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 10, 12, 10)

        head = QHBoxLayout()
        title = QLabel("Serial Monitor")
        f = QFont(); f.setBold(True); f.setPointSize(11)
        title.setFont(f)
        head.addWidget(title)
        head.addStretch(1)

        self.clear_log_btn = QPushButton("Clear")
        self.clear_log_btn.clicked.connect(lambda: self.log_view.clear())
        head.addWidget(self.clear_log_btn)
        lay.addLayout(head)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_LINES)
        mono = QFont("Consolas", 9)
        if not mono.exactMatch():
            mono = QFont("Courier New", 9)
        self.log_view.setFont(mono)
        self.log_view.setMinimumHeight(170)
        lay.addWidget(self.log_view)

        return w

    # =====================================================================
    # Signal wiring
    # =====================================================================
    def _wire_signals(self) -> None:
        # Controller → SerialWorker
        self.controller.send_request.connect(self.worker.write_command)

        # SerialWorker → Controller
        self.worker.line_received.connect(self.controller.on_line_received)

        # SerialWorker → GUI
        self.worker.connected.connect(self._on_serial_connected)
        self.worker.disconnected.connect(self._on_serial_disconnected)
        self.worker.sent.connect(lambda s: self._log("TX", s))
        self.worker.raw_received.connect(
            lambda s: self._log("RX", s.replace("\r", "\\r").replace("\n", "\\n"))
        )
        self.worker.error.connect(lambda m: self._log("ERR", m))

        # Controller → GUI
        self.controller.position_changed.connect(self._on_position_changed)
        self.controller.valve_moving.connect(self._on_valve_moving)
        self.controller.valve_settled.connect(self._on_valve_settled)
        self.controller.busy_changed.connect(self._on_busy_changed)
        self.controller.failure.connect(lambda m: self._log("FAIL", m))
        self.controller.log.connect(self._on_controller_log)

    # =====================================================================
    # Connection management
    # =====================================================================
    def _refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        self.port_combo.clear()
        ports = available_ports()
        if ports:
            self.port_combo.addItems(ports)
            if current in ports:
                self.port_combo.setCurrentText(current)
        else:
            self.port_combo.addItem("(no ports found)")

    def _on_connect_clicked(self) -> None:
        port = self.port_combo.currentText().strip()
        if not port or port.startswith("("):
            self._log("ERR", "Select a valid COM port first")
            return
        self._auto_reconnect_target = port
        self._log("SYS", f"Opening {port} …")
        QMetaObject.invokeMethod(self.worker, "open_port",
                                 Qt.QueuedConnection, Q_ARG(str, port))

    def _on_disconnect_clicked(self) -> None:
        self._auto_reconnect_target = ""
        self._reconnect_timer.stop()
        QMetaObject.invokeMethod(self.worker, "close_port", Qt.QueuedConnection)

    def _try_auto_reconnect(self) -> None:
        if not self._auto_reconnect_target:
            self._reconnect_timer.stop()
            return
        if self._auto_reconnect_target not in available_ports():
            return
        self._log("SYS", f"Auto-reconnecting to {self._auto_reconnect_target}")
        QMetaObject.invokeMethod(self.worker, "open_port", Qt.QueuedConnection,
                                 Q_ARG(str, self._auto_reconnect_target))

    @pyqtSlot(str)
    def _on_serial_connected(self, port_name: str) -> None:
        self._reconnect_timer.stop()
        self._log("SYS", f"Connected to {port_name}")
        self._set_connected_ui(True)
        self.controller.set_connected(True)        # triggers sync_all_positions

    @pyqtSlot(str)
    def _on_serial_disconnected(self, reason: str) -> None:
        self._log("SYS", f"Disconnected: {reason}")
        self._set_connected_ui(False)
        self.controller.set_connected(False)
        if self._auto_reconnect_target:
            self._reconnect_timer.start()

    def _set_connected_ui(self, connected: bool) -> None:
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        self.port_combo.setEnabled(not connected)
        self.refresh_btn.setEnabled(not connected)
        self.sync_btn.setEnabled(connected)
        for vw in self.valve_widgets.values():
            vw.set_io_enabled(connected)
        self.status_pill.set_state("connected" if connected else "disconnected")
        if not connected:
            self.busy_label.setText("")

    # =====================================================================
    # Controller signals → GUI
    # =====================================================================
    @pyqtSlot(int, int)
    def _on_position_changed(self, valve_id: int, port: int) -> None:
        vw = self.valve_widgets.get(valve_id)
        if vw:
            vw.set_active_port(port)

    @pyqtSlot(int, int)
    def _on_valve_moving(self, valve_id: int, target_port: int) -> None:
        vw = self.valve_widgets.get(valve_id)
        if vw:
            vw.set_moving(True, target_port)

    @pyqtSlot(int, int)
    def _on_valve_settled(self, valve_id: int, port: int) -> None:
        vw = self.valve_widgets.get(valve_id)
        if vw:
            vw.set_moving(False)
            vw.set_active_port(port)

    @pyqtSlot(bool)
    def _on_busy_changed(self, busy: bool) -> None:
        # Reflect overall busy state — individual valves still show their own
        # moving/idle indicator from valve_moving / valve_settled.
        if busy:
            self.busy_label.setText("⏳ Busy …")
            if self.disconnect_btn.isEnabled():     # i.e. we are connected
                self.status_pill.set_state("busy")
        else:
            self.busy_label.setText("")
            if self.disconnect_btn.isEnabled():
                self.status_pill.set_state("connected")
            for vw in self.valve_widgets.values():
                vw.set_moving(False)

    @pyqtSlot(str, str)
    def _on_controller_log(self, category: str, message: str) -> None:
        self._log(category.upper(), message)

    # =====================================================================
    # Logging
    # =====================================================================
    _CAT_COLOR = {
        "TX":     "#7fc7ff",
        "RX":     "#a4e070",
        "ERR":    "#ff6b6b",
        "FAIL":   "#ff6b6b",
        "SYS":    "#cfd3d8",
        "TIMEOUT": "#FFB020",
    }

    def _log(self, category: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        color = self._CAT_COLOR.get(category, "#cfd3d8")
        line = (
            f'<span style="color:#7c8089">{ts}</span> '
            f'<span style="color:{color};font-weight:bold">[{category}]</span> '
            f'<span style="color:#e7eaee">{self._escape(message)}</span>'
        )
        self.log_view.appendHtml(line)
        self.log_view.moveCursor(QTextCursor.End)

    @staticmethod
    def _escape(s: str) -> str:
        return (s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))

    # =====================================================================
    # Theming
    # =====================================================================
    def _apply_dark_theme(self) -> None:
        pal = QPalette()
        pal.setColor(QPalette.Window,        QColor("#15181c"))
        pal.setColor(QPalette.Base,          QColor("#1b1f24"))
        pal.setColor(QPalette.AlternateBase, QColor("#21252b"))
        pal.setColor(QPalette.Text,          QColor("#e7eaee"))
        pal.setColor(QPalette.WindowText,    QColor("#e7eaee"))
        pal.setColor(QPalette.Button,        QColor("#2a2f36"))
        pal.setColor(QPalette.ButtonText,    QColor("#e7eaee"))
        pal.setColor(QPalette.Highlight,     QColor("#3a82f7"))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        self.setPalette(pal)

        self.setStyleSheet("""
            QFrame#card, QFrame#header {
                background:#1b1f24;
                border:1px solid #2a2f36;
                border-radius:10px;
            }
            QFrame#header { background:transparent; border:none; }
            QPushButton {
                background:#2a2f36; border:1px solid #3a414a;
                padding:6px 14px; border-radius:6px; color:#e7eaee;
            }
            QPushButton:hover   { background:#343a43; }
            QPushButton:pressed { background:#1f242b; }
            QPushButton:disabled{ color:#6a6f77; background:#23272d; }
            QPushButton#primary {
                background:#F2C74E; color:#11151a; border:1px solid #c9a233;
                font-weight:bold;
            }
            QPushButton#primary:hover    { background:#FFD662; }
            QPushButton#primary:disabled { background:#5c4f23; color:#9b8b54; }
            QToolButton {
                background:#2a2f36; border:1px solid #3a414a;
                border-radius:6px; padding:4px 8px; color:#e7eaee;
            }
            QToolButton:hover { background:#343a43; }
            QComboBox {
                background:#23272d; border:1px solid #3a414a;
                border-radius:6px; padding:4px 8px; color:#e7eaee;
            }
            QPlainTextEdit {
                background:#0f1216; color:#e7eaee;
                border:1px solid #2a2f36; border-radius:6px;
            }
            QLabel { color:#e7eaee; }
            QStatusBar { background:#15181c; color:#9097a0; }
        """)

    # =====================================================================
    # Cleanup
    # =====================================================================
    def closeEvent(self, event):
        try:
            self._reconnect_timer.stop()
            # Ask the worker to stop its timer + close the port on its own
            # thread, then shut the thread down cleanly.
            QMetaObject.invokeMethod(self.worker, "stop", Qt.BlockingQueuedConnection)
            self._thread.quit()
            self._thread.wait(1500)
        finally:
            super().closeEvent(event)
