"""Minimal stand-in for the two physical valves."""
import re, sys, time, threading
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM21"
positions = {1: 1, 2: 1}                       # current port per valve
MOVE_TIME = 0.4                                # simulate mechanical motion

ser = serial.Serial(PORT, 9600, timeout=0.1)
print(f"Simulator listening on {PORT}")
buf = ""

def reply(s):
    ser.write((s + "\r\n").encode())
    print("  TX>", s)

while True:
    chunk = ser.read(64).decode("ascii", "replace")
    if not chunk:
        continue
    buf += chunk
    while "\r" in buf:
        cmd, buf = buf.split("\r", 1)
        cmd = cmd.strip()
        if not cmd:
            continue
        print("RX <", cmd)
        m = re.match(r"/([12])GO(\d+)$", cmd)
        if m:
            v, p = int(m.group(1)), int(m.group(2))
            reply("OK")                         # ack first
            def settle(v=v, p=p):
                time.sleep(MOVE_TIME)
                positions[v] = p
            threading.Thread(target=settle, daemon=True).start()
            continue
        m = re.match(r"/([12])CP$", cmd)
        if m:
            v = int(m.group(1))
            reply(f"Position is = {positions[v]}")
            continue
        reply("ERR")