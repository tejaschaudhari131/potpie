"""
Tiny TCP-based valve simulator.

pyserial supports `socket://host:port` URLs as a stand-in for a real
serial port, so we can drive the actual SerialWorker against this
simulator without any virtual-COM driver.

The simulator implements:
    /1GO<n> /2GO<n>   -> "OK\r\n", then settles internal position after MOVE_TIME
    /1CP /2CP         -> "Position is = <n>\r\n"

Special test hooks (set on the instance):
    .drop_next_n      -> drop that many incoming commands silently
    .reply_garbage    -> reply with "GARBAGE" instead of a position
    .move_time        -> seconds to wait before updating internal position
"""

from __future__ import annotations

import re
import socket
import threading
import time
from typing import Optional


class ValveSimServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(1)
        self._sock.settimeout(0.1)
        self.host, self.port = self._sock.getsockname()

        self.positions = {1: 1, 2: 1}
        self.move_time = 0.05
        self.drop_next_n = 0
        self.reply_garbage = False

        self.commands_received: list[str] = []
        self.replies_sent: list[str] = []

        self._stop = threading.Event()
        self._client: Optional[socket.socket] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> "ValveSimServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._thread.join(timeout=1.0)

    @property
    def url(self) -> str:
        return f"socket://{self.host}:{self.port}"

    # ---- I/O loop ---------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._serve(client)

    def _serve(self, client: socket.socket) -> None:
        self._client = client
        client.settimeout(0.1)
        buf = ""
        try:
            while not self._stop.is_set():
                try:
                    data = client.recv(256)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not data:
                    return
                buf += data.decode("ascii", "replace")
                while "\r" in buf:
                    cmd, buf = buf.split("\r", 1)
                    cmd = cmd.strip()
                    if cmd:
                        self._handle(client, cmd)
        finally:
            try: client.close()
            except Exception: pass
            self._client = None

    # ---- command handler --------------------------------------------------
    def _send(self, client, text: str) -> None:
        payload = (text + "\r\n").encode("ascii")
        client.sendall(payload)
        self.replies_sent.append(text)

    def _handle(self, client: socket.socket, cmd: str) -> None:
        self.commands_received.append(cmd)

        if self.drop_next_n > 0:
            self.drop_next_n -= 1
            return

        m = re.match(r"^/([12])GO(\d+)$", cmd)
        if m:
            v, p = int(m.group(1)), int(m.group(2))
            self._send(client, "OK")
            def settle(v=v, p=p):
                time.sleep(self.move_time)
                self.positions[v] = p
            threading.Thread(target=settle, daemon=True).start()
            return

        m = re.match(r"^/([12])CP$", cmd)
        if m:
            v = int(m.group(1))
            if self.reply_garbage:
                self._send(client, "GARBAGE")
            else:
                self._send(client, f"Position is = {self.positions[v]}")
            return

        # unknown — be silent (real hardware behavior varies)
