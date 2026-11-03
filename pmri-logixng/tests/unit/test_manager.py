"""Unit tests for :class:`LogixNGManager`."""
from __future__ import annotations

import pathlib

import pytest

from pmri_logixng import (
    ConditionalNG,
    ConstantBool,
    LogixNG,
    LogixNGManager,
    SetTurnoutState,
)
from pmri_logixng.actions.digital import LogAction
from pmri_core.state import TurnoutState


def _sample_program(*, enabled: bool = True) -> LogixNG:
    """A minimally-real LogixNG so ``to_yaml`` has more than defaults
    to serialize -- helps catch round-trip bugs the empty-tree case
    would hide."""
    return LogixNG(
        user_name="Yard routine",
        enabled=enabled,
        conditionals=[
            ConditionalNG(
                user_name="Turn out on start",
                actions=[
                    LogAction(message="starting"),
                    SetTurnoutState(turnout="IT1", state=TurnoutState.CLOSED),
                ],
            ),
        ],
    )


async def test_put_and_get() -> None:
    m = LogixNGManager()
    prog = _sample_program()
    await m.put("IQyard", prog)
    assert m.get("IQyard") is prog
    assert list(m) == [prog]
    assert "IQyard" in m
    assert len(m) == 1


async def test_put_second_call_updates() -> None:
    """Replacing at the same name fires ``updated``, not ``added``."""
    m = LogixNGManager()
    seen: list[tuple[str, LogixNG]] = []
    m.added.connect(lambda p: seen.append(("added", p)))
    m.updated.connect(lambda p: seen.append(("updated", p)))
    p1 = _sample_program()
    p2 = _sample_program()
    await m.put("IQx", p1)
    await m.put("IQx", p2)
    assert m.get("IQx") is p2
    assert [tag for tag, _ in seen] == ["added", "updated"]


async def test_create_is_idempotent() -> None:
    m = LogixNGManager()
    a = await m.create("IQnew")
    b = await m.create("IQnew")
    assert a is b


async def test_delete_returns_true_only_on_hit() -> None:
    m = LogixNGManager()
    await m.put("IQx", _sample_program())
    assert await m.delete("IQx") is True
    assert await m.delete("IQx") is False


async def test_delete_emits_removed() -> None:
    m = LogixNGManager()
    prog = _sample_program()
    await m.put("IQx", prog)
    seen: list[LogixNG] = []
    m.removed.connect(lambda p: seen.append(p))
    await m.delete("IQx")
    assert seen == [prog]


async def test_put_rejects_unsafe_names() -> None:
    m = LogixNGManager()
    for bad in ("../oops", "with space", "with/slash", "with\\bs", ""):
        with pytest.raises(ValueError, match="must match"):
            await m.put(bad, _sample_program())


async def test_save_all_and_load_all_roundtrip(tmp_path: pathlib.Path) -> None:
    """The programs on disk after a save can be loaded back into a
    fresh manager with the same contents."""
    root = tmp_path / "logixng"
    m1 = LogixNGManager()
    await m1.put("IQa", _sample_program())
    await m1.put("IQb", _sample_program(enabled=False))
    m1.save_all(root)

    m2 = LogixNGManager()
    count = await m2.load_all(root)
    assert count == 2
    assert m2.get("IQa").user_name == "Yard routine"
    assert m2.get("IQb").enabled is False


async def test_save_all_prunes_stale_files(tmp_path: pathlib.Path) -> None:
    """``save_all`` mirrors the in-memory set -- a program deleted
    since the last save must be removed from disk too, or a reload
    would silently resurrect it."""
    root = tmp_path / "logixng"
    m = LogixNGManager()
    await m.put("IQkeep", _sample_program())
    await m.put("IQdrop", _sample_program())
    m.save_all(root)
    await m.delete("IQdrop")
    m.save_all(root)

    files = {p.name for p in root.iterdir()}
    assert "IQkeep.yaml" in files
    assert "IQdrop.yaml" not in files


async def test_load_all_skips_unsafe_filenames(
    tmp_path: pathlib.Path,
) -> None:
    """A YAML file with a shady name (path separators, spaces) is
    ignored -- the loader must not create a system-name that would
    then be un-writable via :meth:`save_one`."""
    from pmri_logixng.serialize import to_yaml  # noqa: PLC0415

    root = tmp_path / "logixng"
    root.mkdir()
    (root / "IQgood.yaml").write_text(
        to_yaml(_sample_program()), encoding="utf-8",
    )
    # Filename with spaces -- outside the safe regex, must be skipped.
    (root / "with space.yaml").write_text(
        to_yaml(_sample_program()), encoding="utf-8",
    )

    m = LogixNGManager()
    count = await m.load_all(root)
    assert count == 1
    assert "IQgood" in m
    assert "with space" not in m


async def test_load_all_skips_broken_files(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "logixng"
    root.mkdir()
    (root / "IQbroken.yaml").write_text("not: [a, real, program", encoding="utf-8")
    (root / "IQnotroot.yaml").write_text(
        "type: expr:const-bool\nvalue: true\n", encoding="utf-8",
    )
    m = LogixNGManager()
    count = await m.load_all(root)
    assert count == 0


async def test_load_yaml_text_inline() -> None:
    m = LogixNGManager()
    from pmri_logixng.serialize import to_yaml
    text = to_yaml(_sample_program())
    prog = await m.load_yaml_text("IQinline", text)
    assert m.get("IQinline") is prog


async def test_save_one_only_touches_one_file(
    tmp_path: pathlib.Path,
) -> None:
    """``save_one`` doesn't prune stale files (that's ``save_all``'s
    job) -- it just writes the one program's YAML."""
    root = tmp_path / "logixng"
    m = LogixNGManager()
    await m.put("IQa", _sample_program())
    await m.put("IQb", _sample_program())
    m.save_all(root)
    await m.delete("IQa")
    m.save_one(root, "IQb")

    files = {p.name for p in root.iterdir()}
    assert "IQa.yaml" in files    # still there -- save_one doesn't prune
    assert "IQb.yaml" in files


async def test_delete_on_disk(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "logixng"
    m = LogixNGManager()
    await m.put("IQx", _sample_program())
    m.save_all(root)
    m.delete_on_disk(root, "IQx")
    assert not (root / "IQx.yaml").exists()
