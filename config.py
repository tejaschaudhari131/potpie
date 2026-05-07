"""
Application-wide configuration.

Centralising the serial parameters, command templates and valve topology
makes it trivial to add a new valve or change protocol settings without
touching the controller or GUI code.
"""

from dataclasses import dataclass, field
from typing import Dict


# ---------------------------------------------------------------------------
# Serial port defaults (overridable from the GUI later if desired)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SerialConfig:
    baudrate: int = 9600
    bytesize: int = 8
    stopbits: int = 1
    parity: str = "N"          # 'N', 'E', 'O', 'M', 'S'
    timeout: float = 1.0       # read timeout in seconds
    write_timeout: float = 1.0
    terminator: str = "\r"     # ASCII command termination


SERIAL = SerialConfig()


# ---------------------------------------------------------------------------
# Valve definitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ValveSpec:
    valve_id: int                       # numeric address used in protocol
    name: str                           # friendly display name
    num_ports: int                      # number of selectable ports
    color: str                          # accent colour for the GUI
    move_cmd_fmt: str                   # format string, expects {port}
    position_query: str                 # command that returns "Position is = n"


VALVES: Dict[int, ValveSpec] = {
    1: ValveSpec(
        valve_id=1,
        name="Valve-1",
        num_ports=4,
        color="#4FB3C9",                # blue ports (matches reference image)
        move_cmd_fmt="/1GO{port}",
        position_query="/1CP",
    ),
    2: ValveSpec(
        valve_id=2,
        name="Valve-2",
        num_ports=6,
        color="#A4B83A",                # olive/green ports
        move_cmd_fmt="/2GO{port}",
        position_query="/2CP",
    ),
}


# ---------------------------------------------------------------------------
# Behavioural tunables
# ---------------------------------------------------------------------------
RESPONSE_TIMEOUT_MS   = 2000     # how long we wait for a reply per command
RETRY_COUNT           = 2        # number of retries on timeout / bad reply
COMMAND_GAP_MS        = 80       # minimum gap between successive serial writes
AUTO_RECONNECT_MS     = 3000     # interval between auto-reconnect attempts
LOG_MAX_LINES         = 2000     # rolling log buffer
