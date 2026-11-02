"""XML persistence -- load/save of JMRI configuration files.

Stage 1 shipped: per-loco ``locomotive-config`` round-trip
(:mod:`jmri_core.persistence.roster_xml`).

Stage 2 adds:

* :mod:`jmri_core.persistence.profile` -- ``Profile`` and the standard
  ``~/JMRI/`` directory layout.
* :mod:`jmri_core.persistence.roster_index_xml` -- ``roster.xml`` index file
  (per ``roster.xsd``).
* :mod:`jmri_core.persistence.decoder_xml` -- read-only decoder definition
  loader for the 881 ``xml/decoders/*.xml`` files.
* :mod:`jmri_core.persistence.operations_xml` -- semantic round-trip for all
  six Operations module file families.
* :mod:`jmri_core.persistence.panel_xml` -- envelope-level round-trip for
  PanelPro / LayoutEditor files (full element modeling is Stage 11).
"""
from jmri_core.beans.loco_cv_state import LocoCvState
from jmri_core.persistence.backup import (
    DEFAULT_KEEP,
    write_with_backup,
)
from jmri_core.persistence.config_xml import (
    ConfigFile,
    LightRow,
    LightSection,
    SensorRow,
    SensorSection,
    TurnoutRow,
    TurnoutSection,
    empty_config,
    light_mgr_class_for_prefix,
    load_config,
    load_config_bytes,
    save_config,
    save_config_bytes,
    sensor_mgr_class_for_prefix,
    turnout_mgr_class_for_prefix,
)
from jmri_core.persistence.decoder_xml import (
    CompositeChoice,
    CompositeSetting,
    CvBitMask,
    DecoderFamily,
    DecoderFile,
    DecoderIndexEntry,
    DecoderModel,
    DecoderPane,
    DecoderVariable,
    EnumChoice,
    SplitCv,
    UnsupportedVariableKind,
    VariableKind,
    VariableWriteOp,
    find_composite_current_choice,
    iter_decoder_files,
    iter_decoder_index,
    load_all_decoders,
    load_decoder,
    load_decoder_bytes,
    load_pane,
    load_pane_bytes,
    pane_layout_tree,
    resolve_composite_choice,
    variable_apply_to_state,
    variable_cv_list,
    variable_extract_from_state,
    variable_extract_from_store,
    variable_is_indexed,
    variable_primary_cv,
    variable_store_key,
    variable_wire_read_plan,
    variable_wire_write_plan,
)
from jmri_core.persistence.loco_cv_xml import (
    load_cv_values_from_locomotive_config,
    load_cv_values_from_locomotive_config_bytes,
    load_loco_cv,
    load_loco_cv_bytes,
    save_cv_values_into_locomotive_config,
    save_loco_cv,
    save_loco_cv_bytes,
)
from jmri_core.persistence.operations_xml import (
    Car,
    Engine,
    Location,
    OperationsFile,
    Route,
    RouteLocation,
    Train,
    load_operations,
    load_operations_bytes,
    save_operations,
    save_operations_bytes,
)
from jmri_core.persistence.panel_xml import (
    PanelFile,
    load_panel,
    load_panel_bytes,
    save_panel,
    save_panel_bytes,
)
from jmri_core.persistence.pmri_config import (
    PMRI_XML,
    AuthConfig,
    DebugConfig,
    HttpConfig,
    LauncherConfig,
    LogConfig,
    PmriConfig,
    WithrottleConfig,
    XmlRootsConfig,
    load_pmri_config,
    load_pmri_config_bytes,
    pmri_config_path,
    save_pmri_config,
    save_pmri_config_bytes,
)
from jmri_core.persistence.profile import Profile, default_settings_dir
from jmri_core.persistence.profile_registry import (
    ACTIVE_MARKER,
    PROFILES_XML,
    ProfileRef,
    ProfileRegistry,
    load_profile_registry,
    save_profile_registry,
)
from jmri_core.persistence.profile_xml import (
    PROFILE_XML,
    ConnectionDef,
    OpaqueStartupItem,
    ProfileSettings,
    ScriptDef,
    load_profile_xml,
    load_profile_xml_bytes,
    profile_xml_path,
    save_profile_xml,
    save_profile_xml_bytes,
)
from jmri_core.persistence.programmer_xml import (
    ProgrammerConfig,
    iter_programmer_configs,
    load_programmer_config,
    load_programmer_config_bytes,
)
from jmri_core.persistence.roster_index_xml import (
    RosterGroup,
    RosterIndex,
    load_roster_index,
    load_roster_index_bytes,
    save_roster_index,
    save_roster_index_bytes,
)
from jmri_core.persistence.roster_xml import (
    load_locomotive,
    load_locomotive_bytes,
    save_locomotive,
    save_locomotive_bytes,
)

__all__ = [
    # Profile / directory layout
    "Profile",
    "default_settings_dir",
    "ProfileRef",
    "ProfileRegistry",
    "ACTIVE_MARKER",
    "PROFILES_XML",
    "load_profile_registry",
    "save_profile_registry",
    # Backup helper
    "DEFAULT_KEEP",
    "write_with_backup",
    # pmri.xml (install-level launcher config)
    "AuthConfig",
    "DebugConfig",
    "HttpConfig",
    "LauncherConfig",
    "LogConfig",
    "PMRI_XML",
    "PmriConfig",
    "WithrottleConfig",
    "XmlRootsConfig",
    "load_pmri_config",
    "load_pmri_config_bytes",
    "pmri_config_path",
    "save_pmri_config",
    "save_pmri_config_bytes",
    # profile.xml (profile-local connections + scripts)
    "PROFILE_XML",
    "ConnectionDef",
    "OpaqueStartupItem",
    "ProfileSettings",
    "ScriptDef",
    "load_profile_xml",
    "load_profile_xml_bytes",
    "profile_xml_path",
    "save_profile_xml",
    "save_profile_xml_bytes",
    # Per-loco CV state
    "LocoCvState",
    "load_cv_values_from_locomotive_config",
    "load_cv_values_from_locomotive_config_bytes",
    "load_loco_cv",
    "load_loco_cv_bytes",
    "save_cv_values_into_locomotive_config",
    "save_loco_cv",
    "save_loco_cv_bytes",
    # layout-config (Configuration.xml)
    "ConfigFile",
    "LightRow",
    "LightSection",
    "SensorRow",
    "SensorSection",
    "TurnoutRow",
    "TurnoutSection",
    "empty_config",
    "light_mgr_class_for_prefix",
    "load_config",
    "load_config_bytes",
    "save_config",
    "save_config_bytes",
    "sensor_mgr_class_for_prefix",
    "turnout_mgr_class_for_prefix",
    # Per-loco roster
    "load_locomotive",
    "load_locomotive_bytes",
    "save_locomotive",
    "save_locomotive_bytes",
    # Roster index
    "RosterGroup",
    "RosterIndex",
    "load_roster_index",
    "load_roster_index_bytes",
    "save_roster_index",
    "save_roster_index_bytes",
    # Decoder defs
    "CompositeChoice",
    "CompositeSetting",
    "CvBitMask",
    "DecoderFamily",
    "DecoderFile",
    "DecoderIndexEntry",
    "DecoderModel",
    "DecoderPane",
    "DecoderVariable",
    "EnumChoice",
    "SplitCv",
    "UnsupportedVariableKind",
    "VariableKind",
    "iter_decoder_files",
    "iter_decoder_index",
    "load_all_decoders",
    "load_decoder",
    "load_decoder_bytes",
    "load_pane",
    "load_pane_bytes",
    "pane_layout_tree",
    "find_composite_current_choice",
    "resolve_composite_choice",
    "VariableWriteOp",
    "variable_apply_to_state",
    "variable_cv_list",
    "variable_extract_from_state",
    "variable_extract_from_store",
    "variable_is_indexed",
    "variable_primary_cv",
    "variable_store_key",
    "variable_wire_read_plan",
    "variable_wire_write_plan",
    # Programmer definitions (Comprehensive.xml, Basic.xml, ...)
    "ProgrammerConfig",
    "iter_programmer_configs",
    "load_programmer_config",
    "load_programmer_config_bytes",
    # Operations
    "Car",
    "Engine",
    "Location",
    "OperationsFile",
    "Route",
    "RouteLocation",
    "Train",
    "load_operations",
    "load_operations_bytes",
    "save_operations",
    "save_operations_bytes",
    # Panel envelope
    "PanelFile",
    "load_panel",
    "load_panel_bytes",
    "save_panel",
    "save_panel_bytes",
]
