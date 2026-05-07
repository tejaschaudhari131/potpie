"""
Thread-safe serial communication layer.

The SerialWorker runs on a dedicated QThread and is the *only* piece of
code that ever touches the underlying pyserial port. The rest of the
application talks to it via Qt signals/slots which makes the I/O fully
asynchronous and keeps the GUI responsive.

Public surface
--------------
    Signals (out of the worker)
        connected(port_name)
        disconnected(reason)
        line_received(line)            # one parsed ASCII reply (no terminator)
        raw_received(text)             # everything the port produces
        sent(text)                     # echo of every successful write
        error(message)

    Slots (into the worker)
        open_port(port_name)
        close_port()
        write_command(text)
"""

from __future__ import annotations

import time
from typing import Optional

import serial
from serial.tools import list_ports
from PyQt5.QtCore import QObject, QMutex, QMutexLocker, QTimer, pyqtSignal, pyqtSlot

from config import SERIAL


def available_ports() -> list[str]:
    """Return the device names of every serial port currently enumerable."""
    return [p.device for p in list_ports.comports()]


class SerialWorker(QObject):
    # --- outbound signals --------------------------------------------------
    connected      = pyqtSignal(str)
    disconnected   = pyqtSignal(str)
    line_received  = pyqtSignal(str)
    raw_received   = pyqtSignal(str)
    sent           = pyqtSignal(str)
    error          = pyqtSignal(str)

    # --- lifetime ----------------------------------------------------------
    def __init__(self) -> None:
        super().__init__()
        self._port: Optional[serial.Serial] = None
        self._buffer: str = ""
        self._mutex = QMutex()        # guards _port across threads
        self._running = True
        self._poll_timer: Optional[QTimer] = None

    # --------------------------------------------------------------- lifecycle
    @pyqtSlot()
    def start(self) -> None:
        """Run on the worker thread — create the poll timer here so that
        it has the correct thread affinity from the start."""
        if self._poll_timer is None:
            self._poll_timer = QTimer()      # parent stays None on purpose
            self._poll_timer.setInterval(40)
            self._poll_timer.timeout.connect(self.poll)
        self._poll_timer.start()

    @pyqtSlot()
    def stop(self) -> None:
        """Stop the poll timer and close any open port."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
        with QMutexLocker(self._mutex):
            if self._port and self._port.is_open:
                self._safe_close()

    # ------------------------------------------------------------------ slots
    @pyqtSlot(str)
    def open_port(self, port_name: str) -> None:
        with QMutexLocker(self._mutex):
            if self._port and self._port.is_open:
                self._safe_close()

            try:
                # serial_for_url() accepts both plain device names ("COM3",
                # "/dev/ttyUSB0") and URL handlers like "socket://host:port"
                # or "loop://" — the latter are invaluable for testing.
                self._port = serial.serial_for_url(
                    port_name,
                    baudrate=SERIAL.baudrate,
                    bytesize=SERIAL.bytesize,
                    stopbits=SERIAL.stopbits,
                    parity=SERIAL.parity,
                    timeout=SERIAL.timeout,
                    write_timeout=SERIAL.write_timeout,
                    do_not_open=False,
                )
                # Flush any leftover bytes from a previous session
                self._port.reset_input_buffer()
                self._port.reset_output_buffer()
            except (serial.SerialException, ValueError, OSError) as exc:
                self._port = None
                self.error.emit(f"Failed to open {port_name}: {exc}")
                return

        self._buffer = ""
        self.connected.emit(port_name)

    @pyqtSlot()
    def close_port(self) -> None:
        with QMutexLocker(self._mutex):
            if self._port and self._port.is_open:
                self._safe_close()
        self.disconnected.emit("Disconnected by user")

    @pyqtSlot(str)
    def write_command(self, text: str) -> None:
        """Append the configured terminator and ship the command."""
        with QMutexLocker(self._mutex):
            if not (self._port and self._port.is_open):
                self.error.emit("Cannot send: port is not open")
                return
            payload = text + SERIAL.terminator
            try:
                self._port.write(payload.encode("ascii", errors="replace"))
                self._port.flush()
            except (serial.SerialException, serial.SerialTimeoutException, OSError) as exc:
                self._handle_io_failure(f"Write failed: {exc}")
                return
        self.sent.emit(text)

    # ---------------------------------------------------------------- polling
    @pyqtSlot()
    def poll(self) -> None:
        """
        Called repeatedly from a QTimer in the worker thread. Reads any
        pending bytes and emits one signal per line.
        """
        with QMutexLocker(self._mutex):
            if not (self._port and self._port.is_open):
                return
            try:
                waiting = self._port.in_waiting
                chunk = self._port.read(waiting) if waiting else b""
            except (serial.SerialException, OSError) as exc:
                self._handle_io_failure(f"Read failed: {exc}")
                return

        if not chunk:
            return

        text = chunk.decode("ascii", errors="replace")
        self.raw_received.emit(text)
        self._buffer += text

        # Split on either CR or LF so we tolerate any vendor's line ending.
        while True:
            cut = -1
            for sep in ("\r\n", "\n", "\r"):
                idx = self._buffer.find(sep)
                if idx != -1 and (cut == -1 or idx < cut):
                    cut = idx
                    cut_len = len(sep)
            if cut == -1:
                break
            line = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut + cut_len:]
            if line:
                self.line_received.emit(line)

    # ---------------------------------------------------------------- helpers
    def _safe_close(self) -> None:
        try:
            self._port.close()
        except Exception:
            pass
        self._port = None

    def _handle_io_failure(self, message: str) -> None:
        """Common path for read/write exceptions — close & notify."""
        self._safe_close()
        self.error.emit(message)
        self.disconnected.emit(message)
