"""
Industrial-style valve widget.

Each instance shows:
    - the rotor (a coloured circle in the centre with the valve name)
    - one rectangular port button per available port, distributed
      vertically along the chosen side
    - a thick line linking the rotor to the *active* port (illuminated path)
    - hover/disabled states for clear feedback during motion

The widget exposes a `port_clicked(valve_id, port)` Qt signal. It does
not itself talk to the hardware — the controller does that.
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from PyQt5.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QBrush, QFont, QFontMetrics
from PyQt5.QtWidgets import QWidget


# Each Side describes which edge the port column is rendered on.
class Side:
    LEFT  = "left"
    RIGHT = "right"


class ValveWidget(QWidget):
    port_clicked = pyqtSignal(int, int)   # valve_id, port_number

    # ----- geometry constants ---------------------------------------------
    PORT_W            = 110
    PORT_H            = 38
    PORT_GAP          = 18
    ROTOR_RADIUS      = 46
    SIDE_MARGIN       = 30
    LINK_WIDTH_IDLE   = 2
    LINK_WIDTH_ACTIVE = 5

    def __init__(self, valve_id: int, name: str, num_ports: int,
                 accent: str, side: str = Side.LEFT, parent=None):
        super().__init__(parent)
        self.valve_id   = valve_id
        self.name       = name
        self.num_ports  = num_ports
        self.accent     = QColor(accent)
        self.side       = side

        self._active_port: int = 0          # 0 = unknown
        self._target_port: int = 0          # set while moving
        self._is_moving: bool  = False
        self._enabled_io: bool = False      # disabled until COM is open
        self._hover_port: int  = 0

        self.setMouseTracking(True)
        self.setMinimumSize(
            self.PORT_W + self.SIDE_MARGIN * 2 + self.ROTOR_RADIUS * 3,
            num_ports * (self.PORT_H + self.PORT_GAP) + 40,
        )
        self.setFocusPolicy(Qt.NoFocus)

    # ====================================================================
    # State setters (called by the main window)
    # ====================================================================
    def set_active_port(self, port: int) -> None:
        self._active_port = port
        self.update()

    def set_moving(self, moving: bool, target: int = 0) -> None:
        self._is_moving  = moving
        self._target_port = target if moving else 0
        self.update()

    def set_io_enabled(self, enabled: bool) -> None:
        self._enabled_io = enabled
        if not enabled:
            self._active_port = 0
            self._is_moving   = False
        self.update()

    # ====================================================================
    # Geometry helpers
    # ====================================================================
    def _port_rect(self, port_index: int) -> QRectF:
        """`port_index` is zero-based."""
        total_h = self.num_ports * self.PORT_H + (self.num_ports - 1) * self.PORT_GAP
        top = (self.height() - total_h) / 2
        y   = top + port_index * (self.PORT_H + self.PORT_GAP)
        if self.side == Side.LEFT:
            x = self.SIDE_MARGIN
        else:
            x = self.width() - self.SIDE_MARGIN - self.PORT_W
        return QRectF(x, y, self.PORT_W, self.PORT_H)

    def _rotor_center(self) -> QPointF:
        if self.side == Side.LEFT:
            cx = self.width() - self.SIDE_MARGIN - self.ROTOR_RADIUS - 10
        else:
            cx = self.SIDE_MARGIN + self.ROTOR_RADIUS + 10
        return QPointF(cx, self.height() / 2)

    def _hit_test(self, pos) -> int:
        for i in range(self.num_ports):
            if self._port_rect(i).contains(pos):
                return i + 1
        return 0

    # ====================================================================
    # Mouse handling
    # ====================================================================
    def mouseMoveEvent(self, event):
        new_hover = self._hit_test(event.pos())
        if new_hover != self._hover_port:
            self._hover_port = new_hover
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hover_port:
            self._hover_port = 0
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if not self._enabled_io or self._is_moving:
            return
        port = self._hit_test(event.pos())
        if port:
            self.port_clicked.emit(self.valve_id, port)

    # ====================================================================
    # Painting
    # ====================================================================
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        rotor = self._rotor_center()

        # 1) Draw the link from rotor to every port (idle thin lines).
        idle_pen = QPen(QColor("#3b3f45"), self.LINK_WIDTH_IDLE)
        idle_pen.setCapStyle(Qt.RoundCap)
        p.setPen(idle_pen)
        for i in range(self.num_ports):
            r = self._port_rect(i)
            anchor = QPointF(
                r.right() if self.side == Side.LEFT else r.left(),
                r.center().y(),
            )
            p.drawLine(anchor, rotor)

        # 2) Highlight the active path (or target while moving).
        highlight_port = self._target_port if self._is_moving else self._active_port
        if highlight_port:
            hl_color = QColor("#FFB020") if self._is_moving else QColor("#3FD17A")
            hl_pen = QPen(hl_color, self.LINK_WIDTH_ACTIVE)
            hl_pen.setCapStyle(Qt.RoundCap)
            p.setPen(hl_pen)
            r = self._port_rect(highlight_port - 1)
            anchor = QPointF(
                r.right() if self.side == Side.LEFT else r.left(),
                r.center().y(),
            )
            p.drawLine(anchor, rotor)

        # 3) Draw the ports.
        port_font = QFont(self.font())
        port_font.setBold(True)
        port_font.setPointSize(11)
        p.setFont(port_font)

        for i in range(self.num_ports):
            port_num = i + 1
            r = self._port_rect(i)

            is_active = (port_num == self._active_port and not self._is_moving)
            is_target = (port_num == self._target_port and self._is_moving)
            is_hover  = (port_num == self._hover_port and self._enabled_io and not self._is_moving)

            base = QColor(self.accent)
            if is_active:
                fill = base.lighter(115)
                border = QColor("#3FD17A")
                border_w = 3
            elif is_target:
                fill = base.lighter(105)
                border = QColor("#FFB020")
                border_w = 3
            elif is_hover:
                fill = base.lighter(125)
                border = QColor("#1c1f23")
                border_w = 2
            else:
                fill = base
                border = QColor("#1c1f23")
                border_w = 2

            if not self._enabled_io:
                fill = QColor("#5c6066")

            p.setBrush(QBrush(fill))
            p.setPen(QPen(border, border_w))
            p.drawRoundedRect(r, 6, 6)

            p.setPen(QColor("#11151a") if self._enabled_io else QColor("#9097a0"))
            p.drawText(r, Qt.AlignCenter, f"Port-{port_num}")

        # 4) Draw the rotor (centre disc).
        rotor_rect = QRectF(
            rotor.x() - self.ROTOR_RADIUS, rotor.y() - self.ROTOR_RADIUS,
            self.ROTOR_RADIUS * 2, self.ROTOR_RADIUS * 2,
        )

        rotor_fill = QColor("#F2C74E")
        if self._is_moving:
            rotor_fill = QColor("#FFB020")
        elif not self._enabled_io:
            rotor_fill = QColor("#6a6f77")

        p.setBrush(QBrush(rotor_fill))
        p.setPen(QPen(QColor("#1c1f23"), 2))
        p.drawEllipse(rotor_rect)

        # Rotor label
        rotor_font = QFont(self.font())
        rotor_font.setBold(True)
        rotor_font.setPointSize(11)
        p.setFont(rotor_font)
        p.setPen(QColor("#11151a") if self._enabled_io else QColor("#cfd3d8"))
        p.drawText(rotor_rect, Qt.AlignCenter, self.name)

        # Status under rotor
        sub_font = QFont(self.font())
        sub_font.setPointSize(9)
        p.setFont(sub_font)
        if not self._enabled_io:
            status = "offline"
        elif self._is_moving:
            status = f"→ Port-{self._target_port}"
        elif self._active_port:
            status = f"@ Port-{self._active_port}"
        else:
            status = "syncing…"
        p.setPen(QColor("#cfd3d8"))
        sub_rect = QRectF(rotor_rect.left() - 20,
                          rotor_rect.bottom() + 4,
                          rotor_rect.width() + 40, 18)
        p.drawText(sub_rect, Qt.AlignCenter, status)

        p.end()
