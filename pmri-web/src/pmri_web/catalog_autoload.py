"""Auto-load the decoder catalog at startup when ``JMRI_DECODER_XML_ROOT``
is set.

The manual load path (``POST /programmer/catalog/load``) runs a
worker thread + progress polling because a fresh scan of JMRI's
decoder tree takes 30-60 s on a Pi. We reuse that pattern here:
the ``schedule_autoload`` helper kicks off the same worker in the
background and returns immediately, so uvicorn's lifespan startup
doesn't block on it.

Progress is recorded on ``handle.ctx._catalog_load_progress`` in the
same shape the manual load uses (``{loaded, total, done, error,
directory}``). The programmer page renders the progress bar when
this dict is present and ``done`` is False -- so a user who lands on
``/programmer`` while the autoload is running sees "Loading catalog..."
with a live thermometer instead of the stale "Catalog empty" hint
that would tempt them to click Load again.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Any

_log = logging.getLogger(__name__)


def _load_catalog_sync(
    target: pathlib.Path,
    progress: dict[str, Any] | None = None,
) -> Any:
    """Do the actual catalog scan. Blocking -- call from a worker
    thread. If ``progress`` is passed, it's updated in place with
    per-file counters so the progress-poll endpoint can render the
    thermometer live."""
    from pmri_core.persistence.decoder_xml import (  # noqa: PLC0415
        iter_decoder_index,
    )
    from pmri_decoderpro import DecoderCatalog  # noqa: PLC0415

    catalog = DecoderCatalog()
    seen_files: set[pathlib.Path] = set()
    for entry in iter_decoder_index(target):
        catalog.add_index_entry(entry)
        if entry.source_file not in seen_files:
            seen_files.add(entry.source_file)
            if progress is not None:
                progress["loaded"] = len(seen_files)
    if progress is not None and progress.get("total"):
        # Match the total so the thermometer reads 100% once we're
        # past the scan phase (before the follow-up mfg_ids load).
        progress["loaded"] = progress["total"]

    programmers_dir = target.parent / "programmers"
    if programmers_dir.is_dir():
        from pmri_core.persistence.programmer_xml import (  # noqa: PLC0415
            iter_programmer_configs,
        )
        catalog.programmers = {
            pc.name: pc for pc in iter_programmer_configs(programmers_dir)
        }

    index_path = target.parent / "decoderIndex.xml"
    if index_path.is_file():
        from pmri_decoderpro.catalog import _load_mfg_ids  # noqa: PLC0415
        catalog.mfg_ids = _load_mfg_ids(index_path)
    return catalog


def schedule_autoload(handle: Any) -> asyncio.Task[None] | None:
    """If ``JMRI_DECODER_XML_ROOT`` is set to a real directory, kick
    off a background catalog load. Returns the task so callers can
    await it in tests, or ``None`` when no autoload was scheduled
    (env unset, dir missing, decoderpro not installed).

    Idempotent -- a subsequent manual load through the /programmer
    tab replaces the catalog atomically. Errors are logged, marked
    on the progress dict, and swallowed so a bad env var doesn't
    break server startup.
    """
    from pmri_core.env import decoder_xml_root  # noqa: PLC0415

    target = decoder_xml_root()
    if target is None:
        return None
    if not target.is_dir():
        _log.warning(
            "JMRI_DECODER_XML_ROOT=%s: not a directory; skipping autoload",
            target,
        )
        return None

    # Precount XML files so the thermometer has a denominator. This
    # is a synchronous glob -- cheap on a Pi even for the full
    # ~1200-file JMRI decoder tree (single dirent walk, no parsing).
    try:
        total = sum(1 for _ in target.rglob("*.xml"))
    except OSError as e:
        _log.warning("autoload: couldn't precount %s: %s", target, e)
        total = 0

    progress: dict[str, Any] = {
        "loaded": 0,
        "total": total,
        "done": False,
        "error": None,
        "directory": str(target),
    }
    handle.ctx._catalog_load_progress = progress  # type: ignore[attr-defined]

    async def _run() -> None:
        try:
            catalog = await asyncio.to_thread(
                _load_catalog_sync, target, progress,
            )
        except Exception as e:  # noqa: BLE001
            _log.exception(
                "decoder catalog autoload failed for %s", target,
            )
            progress["error"] = str(e)
            progress["done"] = True
            return
        handle.ctx._decoder_catalog = catalog  # type: ignore[attr-defined]
        progress["done"] = True
        _log.info(
            "decoder catalog auto-loaded from %s: %d entr%s",
            target, len(catalog),
            "y" if len(catalog) == 1 else "ies",
        )

    return asyncio.create_task(_run(), name="decoder-catalog-autoload")
