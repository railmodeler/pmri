from __future__ import annotations

import pytest

from pmri_nce.command_set import (
    COMMAND_SETS,
    NCE_PH_PRO_2006,
    NCE_POWERCAB,
    NCE_SB5,
    get_command_set,
)


def test_three_canonical_command_sets_present() -> None:
    assert "PH-Pro 2006+" in COMMAND_SETS
    assert "PowerCab" in COMMAND_SETS
    assert "Smart Booster SB5" in COMMAND_SETS


def test_lookup_by_name() -> None:
    assert get_command_set("PH-Pro 2006+") is NCE_PH_PRO_2006
    assert get_command_set("PowerCab") is NCE_POWERCAB
    assert get_command_set("Smart Booster SB5") is NCE_SB5


def test_unknown_name_raises() -> None:
    with pytest.raises(KeyError, match="unknown NCE command set"):
        get_command_set("totally made up CS")


def test_ph_pro_supports_aiu_powercab_does_not() -> None:
    assert NCE_PH_PRO_2006.aiu_polling_supported is True
    assert NCE_POWERCAB.aiu_polling_supported is False
    assert NCE_SB5.aiu_polling_supported is True


def test_command_sets_are_frozen() -> None:
    with pytest.raises((AttributeError, TypeError)):
        NCE_PH_PRO_2006.name = "x"  # type: ignore[misc]
