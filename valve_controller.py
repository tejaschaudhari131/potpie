"""
High-level valve control logic.

Implements the requested state machine:
        IDLE → SEND_COMMAND → WAIT_FOR_RESPONSE → VERIFY_POSITION
             → UPDATE_GUI → IDLE

A single FIFO command queue serialises all activity. While a transaction
is in progress every other request is either queued (port moves) or
collapsed (only one position-query per valve in flight at a time), so
rapid clicks can never desynchronise the hardware.

The controller is GUI-agnostic — it only emits Qt signals. The window
class is responsible for translating those into pixels.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Deque, Dict, Optional

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from config import VALVES, RESPONSE_TIMEOUT_MS, RETRY_COUNT, COMMAND_GAP_MS


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------
class _Kind(Enum):
    MOVE  = auto()
    QUERY = auto()


class _State(Enum):
    IDLE              = auto()
    SEND_COMMAND      = auto()
    WAIT_FOR_RESPONSE = auto()
    VERIFY_POSITION   = auto()


@dataclass
class _Txn:
    valve_id: int
    kind: _Kind
    command: str
    target_port: Optional[int] = None     # only for MOVE
    attempts: int = 0


# Matches "Position is = 3", "position is = 12", " Position is=4 ", etc.
_POS_RE = re.compile(r"position\s*is\s*=\s*(\d+)", re.IGNORECASE)


class ValveController(QObject):
    """
    Owns the per-valve state and orchestrates serial transactions.
    """

    # ---- signals to the GUI -----------------------------------------------
    busy_changed       = pyqtSignal(bool)             # True while a txn is active
    position_changed   = pyqtSignal(int, int)         # valve_id, port (0 = unknown)
    valve_moving       = pyqtSignal(int, int)         # valve_id, target_port
    valve_settled      = pyqtSignal(int, int)         # valve_id, port
    log                = pyqtSignal(str, str)         # category, message
    failure            = pyqtSignal(str)              # human-readable error

    # ---- signals to the SerialWorker --------------------------------------
    send_request       = pyqtSignal(str)

    # -----------------------------------------------------------------------
    def __init__(self) -> None:
        super().__init__()
        self._positions: Dict[int, int] = {vid: 0 for vid in VALVES}
        self._queue: Deque[_Txn] = deque()
        self._current: Optional[_Txn] = None
        self._state: _State = _State.IDLE

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)

        self._gap = QTimer(self)
        self._gap.setSingleShot(True)
        self._gap.timeout.connect(self._pump)

        self._connected = False

    # =======================================================================
    # Public API used by the GUI
    # =======================================================================
    def position(self, valve_id: int) -> int:
        return self._positions.get(valve_id, 0)

    def is_busy(self) -> bool:
        return self._state is not _State.IDLE or bool(self._queue)

    @pyqtSlot(bool)
    def set_connected(self, connected: bool) -> None:
        """Called when the serial worker reports (dis)connection."""
        self._connected = connected
        if not connected:
            # Tear down any in-flight work; positions become unknown.
            self._timeout.stop()
            self._queue.clear()
            self._current = None
            self._state = _State.IDLE
            for vid in self._positions:
                self._positions[vid] = 0
                self.position_changed.emit(vid, 0)
            self.busy_changed.emit(False)
        else:
            # Sync GUI with real hardware state on (re)connect.
            self.sync_all_positions()

    @pyqtSlot(int, int)
    def request_move(self, valve_id: int, target_port: int) -> None:
        spec = VALVES.get(valve_id)
        if not spec:
            self.failure.emit(f"Unknown valve id {valve_id}")
            return
        if not (1 <= target_port <= spec.num_ports):
            self.failure.emit(f"{spec.name}: port {target_port} is out of range")
            return
        if not self._connected:
            self.failure.emit("Not connected to a COM port")
            return

        # Collapse only consecutive duplicates: if the *most recent* known
        # target for this valve is already the same port, ignore the click.
        # That keeps user intent for sequences like 3 → 4 → 3 (all distinct)
        # while still rejecting click-spam on the same button.
        last_target = self._last_known_target(valve_id)
        if last_target == target_port:
            return

        cmd = spec.move_cmd_fmt.format(port=target_port)
        self._queue.append(_Txn(valve_id, _Kind.MOVE, cmd, target_port))
        self.valve_moving.emit(valve_id, target_port)
        self._update_busy()
        self._pump()

    @pyqtSlot(int)
    def query_position(self, valve_id: int) -> None:
        spec = VALVES.get(valve_id)
        if not spec or not self._connected:
            return
        # Avoid stacking redundant queries.
        for t in self._queue:
            if t.kind is _Kind.QUERY and t.valve_id == valve_id:
                return
        self._queue.append(_Txn(valve_id, _Kind.QUERY, spec.position_query))
        self._update_busy()
        self._pump()

    def sync_all_positions(self) -> None:
        for vid in VALVES:
            self.query_position(vid)

    def _last_known_target(self, valve_id: int) -> int:
        """Return the most recent move target for this valve — looking at
        (1) the last queued move, then (2) the in-flight move, then
        (3) the current verified position."""
        for t in reversed(self._queue):
            if t.kind is _Kind.MOVE and t.valve_id == valve_id:
                return t.target_port or 0
        if (self._current and self._current.valve_id == valve_id
                and self._current.kind is _Kind.MOVE):
            return self._current.target_port or 0
        return self._positions.get(valve_id, 0)

    # =======================================================================
    # State machine internals
    # =======================================================================
    def _pump(self) -> None:
        """Advance the state machine if possible."""
        if self._state is not _State.IDLE:
            return
        if not self._queue:
            self._update_busy()
            return
        if not self._connected:
            self._queue.clear()
            self._update_busy()
            return

        self._current = self._queue.popleft()
        self._state = _State.SEND_COMMAND
        self._dispatch_current()

    def _dispatch_current(self) -> None:
        assert self._current is not None
        self._current.attempts += 1
        self._state = _State.WAIT_FOR_RESPONSE
        self._timeout.start(RESPONSE_TIMEOUT_MS)
        self.send_request.emit(self._current.command)

    @pyqtSlot(str)
    def on_line_received(self, line: str) -> None:
        """Hooked to SerialWorker.line_received."""
        if self._state is not _State.WAIT_FOR_RESPONSE or self._current is None:
            # Unsolicited data: keep it in the log, but do not act on it.
            self.log.emit("rx-unsol", line)
            return

        txn = self._current
        if txn.kind is _Kind.MOVE:
            # The vendor protocol typically emits an ack first; the verifying
            # CP query carries the authoritative position. We accept any reply
            # here and immediately follow up with a CP.
            self._timeout.stop()
            self._state = _State.VERIFY_POSITION
            spec = VALVES[txn.valve_id]
            verify = _Txn(txn.valve_id, _Kind.QUERY, spec.position_query,
                          target_port=txn.target_port)
            # Run verify next, ahead of any newly queued user actions.
            self._queue.appendleft(verify)
            self._current = None
            self._state = _State.IDLE
            self._gap.start(COMMAND_GAP_MS)
            return

        # QUERY transaction — parse "Position is = N"
        match = _POS_RE.search(line)
        if not match:
            self.log.emit("rx-bad", f"Could not parse position reply: {line!r}")
            self._retry_or_fail("Malformed position reply")
            return

        pos = int(match.group(1))
        self._timeout.stop()
        self._positions[txn.valve_id] = pos
        self.position_changed.emit(txn.valve_id, pos)

        if txn.target_port is not None and pos != txn.target_port:
            self.log.emit(
                "verify-fail",
                f"Valve-{txn.valve_id} reported port {pos}, expected {txn.target_port}",
            )
            self.failure.emit(
                f"Valve-{txn.valve_id} did not reach port {txn.target_port} "
                f"(currently on port {pos})"
            )
        elif txn.target_port is not None:
            self.valve_settled.emit(txn.valve_id, pos)

        self._current = None
        self._state = _State.IDLE
        self._gap.start(COMMAND_GAP_MS)

    # ---------------------------------------------------------------- timeouts
    def _on_timeout(self) -> None:
        if self._current is None:
            return
        self.log.emit("timeout",
                      f"No reply for {self._current.command!r} "
                      f"(attempt {self._current.attempts}/{RETRY_COUNT + 1})")
        self._retry_or_fail("Communication timeout")

    def _retry_or_fail(self, reason: str) -> None:
        if self._current is None:
            self._state = _State.IDLE
            self._pump()
            return

        if self._current.attempts <= RETRY_COUNT:
            # Re-arm and try again.
            self._state = _State.SEND_COMMAND
            self._dispatch_current()
            return

        # Out of retries.
        failed = self._current
        self._current = None
        self._state = _State.IDLE
        self.failure.emit(
            f"{reason} on Valve-{failed.valve_id} "
            f"(command {failed.command!r})"
        )
        self._gap.start(COMMAND_GAP_MS)

    # ---------------------------------------------------------------- misc
    def _update_busy(self) -> None:
        self.busy_changed.emit(self.is_busy())
