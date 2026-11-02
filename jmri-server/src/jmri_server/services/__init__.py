"""Per-topic JSON services.

Importing this package returns the canonical list of services. Use
:func:`register_all` to register them on a :class:`ServiceRegistry`.
"""
from jmri_server.services.beans import (
    BlockService,
    LightService,
    MemoryService,
    ReporterService,
    RouteService,
    SensorService,
    SignalHeadService,
    SignalMastService,
    TurnoutService,
)
from jmri_server.services.audio import AudioService
from jmri_server.services.bean_transfer import BeanTransferService
from jmri_server.services.config_profile import ConfigProfileService
from jmri_server.services.config_user import ConfigUserService
from jmri_server.services.consist import ConsistService
from jmri_server.services.fast_clock import TimeService
from jmri_server.services.id_tag import IdTagService
from jmri_server.services.layout_block import LayoutBlockService
from jmri_server.services.logixng import LogixNGService
from jmri_server.services.logs import LogsService
from jmri_server.services.meta import (
    HelloService,
    NodeService,
    PingService,
    PowerService,
    RailroadService,
    VersionService,
)
from jmri_server.services.openlcb import OpenLcbCSService, OpenLcbService
from jmri_server.services.oblock import OBlockService
from jmri_server.services.panel import PanelService
from jmri_server.services.programmer import ProgrammerService
from jmri_server.services.roster import RosterService
from jmri_server.services.scripts import ScriptsService
from jmri_server.services.signal_system import SignalSystemService
from jmri_server.services.throttle import ThrottleService
from jmri_server.services.util import (
    MetadataService,
    NetworkServiceService,
    SystemConnectionService,
)

ALL_SERVICES = [
    HelloService,
    NodeService,
    PingService,
    VersionService,
    RailroadService,
    PowerService,
    TurnoutService,
    SensorService,
    LightService,
    SignalHeadService,
    SignalMastService,
    MemoryService,
    ReporterService,
    BlockService,
    RouteService,
    RosterService,
    ThrottleService,
    ProgrammerService,
    ScriptsService,
    TimeService,
    ConsistService,
    MetadataService,
    NetworkServiceService,
    SystemConnectionService,
    AudioService,
    IdTagService,
    LayoutBlockService,
    OBlockService,
    PanelService,
    LogixNGService,
    LogsService,
    SignalSystemService,
    BeanTransferService,
    ConfigProfileService,
    ConfigUserService,
    OpenLcbService,
    OpenLcbCSService,
]


def register_all(registry: "ServiceRegistry") -> list[object]:
    return [registry.register(s) for s in ALL_SERVICES]


from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from jmri_server.registry import ServiceRegistry


__all__ = [
    "ALL_SERVICES",
    "AudioService",
    "BeanTransferService",
    "BlockService",
    "ConfigProfileService",
    "ConfigUserService",
    "ConsistService",
    "HelloService",
    "IdTagService",
    "LayoutBlockService",
    "LogixNGService",
    "LogsService",
    "LightService",
    "MemoryService",
    "MetadataService",
    "NetworkServiceService",
    "NodeService",
    "OBlockService",
    "OpenLcbCSService",
    "OpenLcbService",
    "PanelService",
    "PingService",
    "PowerService",
    "ProgrammerService",
    "RailroadService",
    "ReporterService",
    "RosterService",
    "RouteService",
    "ScriptsService",
    "SensorService",
    "SignalHeadService",
    "SignalMastService",
    "SignalSystemService",
    "SystemConnectionService",
    "ThrottleService",
    "TimeService",
    "TurnoutService",
    "VersionService",
    "register_all",
]
