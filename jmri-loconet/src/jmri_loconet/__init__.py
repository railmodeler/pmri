"""jmri-loconet -- Stage 4 of the JMRI Python rewrite.

The first real hardware driver. Subclass of ``jmri_core.hal.SystemConnection``
that drives the LocoNet wire protocol (Digitrax). Registered via
``[project.entry-points."jmri.connection"] loconet`` so the core plugin
discovery picks it up after a ``pip install``.
"""
from jmri_loconet.beans import (
    LnPowerManager,
    LnSensor,
    LnSensorManager,
    LnTurnout,
    LnTurnoutManager,
)
from jmri_loconet.connection import LocoNetConnection
from jmri_loconet.programmer import (
    LnOpsProgrammer,
    LnProgrammer,
    LnProgrammerManager,
    LnServiceProgrammer,
)
from jmri_loconet.slots import Slot, SlotManager
from jmri_loconet.throttle import LnThrottle, LnThrottleManager
from jmri_loconet.traffic import LocoNetTrafficController
from jmri_loconet.transport import LocoNetPort, LoopbackPort, SerialPort, TcpPort, loopback_pair

__version__ = "0.1.0"

__all__ = [
    "LnOpsProgrammer",
    "LnPowerManager",
    "LnProgrammer",
    "LnProgrammerManager",
    "LnSensor",
    "LnSensorManager",
    "LnServiceProgrammer",
    "LnThrottle",
    "LnThrottleManager",
    "LnTurnout",
    "LnTurnoutManager",
    "LocoNetConnection",
    "LocoNetPort",
    "LocoNetTrafficController",
    "LoopbackPort",
    "SerialPort",
    "Slot",
    "SlotManager",
    "TcpPort",
    "__version__",
    "loopback_pair",
]
