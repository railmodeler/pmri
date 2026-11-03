"""Per-topic JSON services.

Importing this package returns the canonical list of services. Use
:func:`register_all` to register them on a :class:`ServiceRegistry`.
"""
from pmri_server.services.beans import (
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
from pmri_server.services.audio import AudioService
from pmri_server.services.bean_transfer import BeanTransferService
from pmri_server.services.config_profile import ConfigProfileService
from pmri_server.services.config_user import ConfigUserService
from pmri_server.services.consist import ConsistService
from pmri_server.services.fast_clock import TimeService
from pmri_server.services.id_tag import IdTagService
from pmri_server.services.layout_block import LayoutBlockService
from pmri_server.services.logixng import LogixNGService
from pmri_server.services.logs import LogsService
from pmri_server.services.meta import (
    HelloService,
    NodeService,
    PingService,
    PowerService,
    RailroadService,
    VersionService,
)
from pmri_server.services.openlcb import OpenLcbCSService, OpenLcbService
from pmri_server.services.oblock import OBlockService
from pmri_server.services.panel import PanelService
from pmri_server.services.programmer import ProgrammerService
from pmri_server.services.roster import RosterService
from pmri_server.services.scripts import ScriptsService
from pmri_server.services.signal_system import SignalSystemService
from pmri_server.services.throttle import ThrottleService
from pmri_server.services.util import (
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
    from pmri_server.registry import ServiceRegistry


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
