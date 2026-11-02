"""jmri-ncemonitor -- passive NCE Cab Bus monitor driver.

The HAL stress test: a fully read-only driver. Where LocoNet is async
broadcast and NCE is sync request/response, this is pure observation --
no outbound traffic at all. Demonstrates that the SystemConnection ABC
gracefully supports passive drivers.
"""
from jmri_ncemonitor.beans import (
    PassivePowerManager,
    PassiveSensor,
    PassiveSensorManager,
    PassiveTurnout,
    PassiveTurnoutManager,
)
from jmri_ncemonitor.codec import (
    MAX_BODY_BYTES,
    BodyOverflowError,
)
from jmri_ncemonitor.connection import NceMonitorConnection
from jmri_ncemonitor.messages import (
    AccessoryObserved,
    AiuUpdate,
    CabBusMessage,
    CabBusUnknown,
    CabPoll,
    CabResponse,
    ConsistObserved,
    FastClockUpdate,
    MacroObserved,
    MasterAck,
    ProgrammingResponse,
    ThrottleObserved,
)
from jmri_ncemonitor.monitor import CabBusMonitor
from jmri_ncemonitor.parity9bit import Parity9BitDecoder
from jmri_ncemonitor.replay import (
    BusCapture,
    BusReplay,
    CapturedFrame,
    make_replay_pair,
)
from jmri_ncemonitor.transport import BytePort, LoopbackPort, SerialPort, loopback_pair

__version__ = "0.1.0"

__all__ = [
    "AccessoryObserved",
    "AiuUpdate",
    "BodyOverflowError",
    "BusCapture",
    "BusReplay",
    "BytePort",
    "CabBusMessage",
    "CabBusMonitor",
    "CabBusUnknown",
    "CabPoll",
    "CabResponse",
    "CapturedFrame",
    "ConsistObserved",
    "FastClockUpdate",
    "LoopbackPort",
    "MAX_BODY_BYTES",
    "MacroObserved",
    "MasterAck",
    "NceMonitorConnection",
    "Parity9BitDecoder",
    "PassivePowerManager",
    "PassiveSensor",
    "PassiveSensorManager",
    "PassiveTurnout",
    "PassiveTurnoutManager",
    "ProgrammingResponse",
    "SerialPort",
    "ThrottleObserved",
    "__version__",
    "loopback_pair",
    "make_replay_pair",
]
