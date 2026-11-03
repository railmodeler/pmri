"""pmri-nce -- Stage 4 second hardware driver (NCE).

Where :mod:`pmri_loconet` exercises the HAL with an async-broadcast wire
protocol, this package exercises it with a synchronous request/response
protocol plus polling. Both drivers plug into the same
``SystemConnection`` ABC without changes to ``pmri-core`` or ``pmri-server``
-- the abstraction earned its keep.
"""
from pmri_nce.beans import (
    NcePowerManager,
    NceSensor,
    NceSensorManager,
    NceTurnout,
    NceTurnoutManager,
)
from pmri_nce.command_set import (
    COMMAND_SETS,
    NCE_PH_PRO_2006,
    NCE_POWERCAB,
    NCE_SB5,
    NceCommandSet,
    get_command_set,
)
from pmri_nce.connection import NceConnection
from pmri_nce.detect import detect_command_set, model_to_command_set
from pmri_nce.messages import NceCommand, NceCommandError, NceReply
from pmri_nce.monitors import NceAiuPoller, NceTurnoutMonitor
from pmri_nce.programmer import (
    NceProgrammer,
    NceProgrammerManager,
    NceServiceProgrammer,
)
from pmri_nce.throttle import NceThrottle, NceThrottleManager
from pmri_nce.traffic import NceTrafficController
from pmri_nce.transport import BytePort, LoopbackPort, SerialPort, loopback_pair

__version__ = "0.1.0"

__all__ = [
    "COMMAND_SETS",
    "BytePort",
    "LoopbackPort",
    "NCE_PH_PRO_2006",
    "NCE_POWERCAB",
    "NCE_SB5",
    "NceAiuPoller",
    "NceCommand",
    "NceCommandError",
    "NceCommandSet",
    "NceConnection",
    "NcePowerManager",
    "NceProgrammer",
    "NceProgrammerManager",
    "NceReply",
    "NceServiceProgrammer",
    "NceSensor",
    "NceSensorManager",
    "NceThrottle",
    "NceThrottleManager",
    "NceTrafficController",
    "NceTurnout",
    "NceTurnoutManager",
    "NceTurnoutMonitor",
    "SerialPort",
    "__version__",
    "detect_command_set",
    "get_command_set",
    "loopback_pair",
    "model_to_command_set",
]
