// Client-side dirty tracking + batch write orchestration for the
// programmer page. Wires together: the dirty state per row, the
// pane-level Write / Cancel buttons, and the /write-batch /
// /batch/status polling endpoints.
//
// Dirty state is keyed by (locoId, item) and persists across tab
// swaps so "Write all dirty" spans panes — the user can edit rows on
// Basic, switch to Motor, edit more, then write everything at once.
// Tab-swap rehydration re-marks rows whose data-item lives in the
// dirty map after HTMX replaces the pane content.

(function () {
  "use strict";

  // { locoId: { item: { cv, value, item, paneSlug, kind } } }
  const dirtyByLoco = {};

  function paneRoot() {
    return document.querySelector(".programmer-pane");
  }

  function locoIdFromPane(pane) {
    return pane ? pane.dataset.locoId : null;
  }

  function paneSlug(pane) {
    return pane ? pane.dataset.paneSlug : null;
  }

  function fileName(pane) {
    return pane ? (pane.dataset.fileName || "") : "";
  }

  function readCurrentValue(row) {
    // data-current is the last-written / last-read value (from server).
    // Empty string means "no known value yet".
    const cur = row.dataset.current;
    if (cur === "" || cur === undefined) return null;
    const n = parseInt(cur, 10);
    return Number.isNaN(n) ? cur : n;
  }

  function readInputValue(input) {
    if (!input) return null;
    if (input.type === "checkbox") {
      return input.checked ? 1 : 0;
    }
    if (input.tagName === "SELECT") {
      return input.value;
    }
    const raw = input.value;
    if (raw === "") return null;
    const n = parseInt(raw, 10);
    return Number.isNaN(n) ? raw : n;
  }

  // Compare an input's current value to the stored value. Numbers are
  // compared numerically so "3" vs 3 doesn't spuriously flip dirty.
  function inputIsDirty(row, input) {
    const cur = readCurrentValue(row);
    const now = readInputValue(input);
    if (now === null && cur === null) return false;
    return String(now) !== String(cur);
  }

  function updateRowDirtyState(row) {
    const pane = row.closest(".programmer-pane");
    if (!pane) return;
    const locoId = locoIdFromPane(pane);
    const input = row.querySelector(".cv-value-input");
    const item = row.dataset.item;
    if (!locoId || !input || !item) return;

    const isDirty = inputIsDirty(row, input);
    row.classList.toggle("dirty", isDirty);

    const perLoco = dirtyByLoco[locoId] || (dirtyByLoco[locoId] = {});
    if (isDirty) {
      const kind = row.dataset.kind || "";
      const entry = {
        item,
        cv: parseInt(row.dataset.cv, 10),
        value: readInputValue(input),
        paneSlug: paneSlug(pane),
        kind,
      };
      // Composite variables fan out into multiple sub-variable
      // writes on submit. Grab the selected <option>'s ``data-settings``
      // JSON so ``toRequestPayload`` can expand into per-sub-variable
      // BatchWrite entries without needing to re-derive them at send
      // time. A "(custom)" pick (empty value) means the user unset the
      // preset — no sub-writes.
      if (kind === "composite" && input.tagName === "SELECT") {
        const opt = input.options[input.selectedIndex];
        try {
          entry.compositeSettings = opt && opt.dataset.settings
            ? JSON.parse(opt.dataset.settings)
            : [];
        } catch (_) {
          entry.compositeSettings = [];
        }
      }
      perLoco[item] = entry;
    } else {
      delete perLoco[item];
    }
    refreshDirtyCounter(pane);
  }

  function countDirtyOnPane(pane) {
    // Variable rows are either widget divs (rich pane layout) or
    // ``<tr>`` (speed table). Both carry data-item; both get the
    // ``dirty`` class when the user edits their input. Raw CV rows
    // (data-cv-name) have their own tracking and aren't counted.
    return pane.querySelectorAll("[data-item].dirty").length;
  }

  function countAllDirty(locoId) {
    return Object.keys(dirtyByLoco[locoId] || {}).length;
  }

  function refreshDirtyCounter(pane) {
    const locoId = locoIdFromPane(pane);
    const paneCount = countDirtyOnPane(pane);
    const allCount = countAllDirty(locoId);

    const label = pane.querySelector('[data-role="dirty-count"]');
    if (label) {
      label.textContent = allCount === 0
        ? "0 changed"
        : `${paneCount} on this pane · ${allCount} total`;
    }
    // Selector must be scoped to the write buttons so we don't also
    // toggle the Read pane button (which is always enabled — reads
    // don't depend on dirty state).
    const paneBtn = pane.querySelector('.pane-write-btn[data-scope="pane"]');
    const allBtn = pane.querySelector('.pane-write-btn[data-scope="all"]');
    if (paneBtn) paneBtn.disabled = paneCount === 0;
    if (allBtn) allBtn.disabled = allCount === 0;
  }

  // Re-mark rows whose item is in the dirty map. Called after every
  // HTMX pane swap so switching Basic → Motor → Basic preserves the
  // user's edits on Basic.
  function rehydrateDirty(pane) {
    const locoId = locoIdFromPane(pane);
    const perLoco = dirtyByLoco[locoId] || {};
    pane.querySelectorAll("[data-item]").forEach(row => {
      const item = row.dataset.item;
      const entry = perLoco[item];
      if (!entry) return;
      row.classList.add("dirty");
      const input = row.querySelector(".cv-value-input");
      if (input && entry.value !== null && entry.value !== undefined) {
        // Restore the pending edit so the field shows what the user
        // typed before switching away from this pane.
        if (input.type === "checkbox") {
          input.checked = !!entry.value;
        } else {
          input.value = entry.value;
        }
      }
    });
    refreshDirtyCounter(pane);
  }

  // ------------------------------------------------------------------
  // Batch writes
  // ------------------------------------------------------------------

  let pollTimer = null;

  function stopPolling() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  }

  function showProgress(pane, msg) {
    const el = pane.querySelector('[data-role="batch-progress"]');
    if (el) el.hidden = false;
    setStatus(pane, msg);
    const cancelBtn = pane.querySelector(".pane-cancel-btn");
    if (cancelBtn) cancelBtn.hidden = false;
  }

  function hideProgress(pane) {
    const el = pane.querySelector('[data-role="batch-progress"]');
    if (el) el.hidden = true;
    const cancelBtn = pane.querySelector(".pane-cancel-btn");
    if (cancelBtn) cancelBtn.hidden = true;
  }

  function setStatus(pane, msg) {
    const el = pane.querySelector('[data-role="batch-status"]');
    if (el) el.textContent = msg;
  }

  function setFill(pane, pct) {
    const el = pane.querySelector('[data-role="batch-fill"]');
    if (el) el.style.width = `${pct}%`;
  }

  function collectWritesForPane(pane) {
    const locoId = locoIdFromPane(pane);
    const slug = paneSlug(pane);
    const perLoco = dirtyByLoco[locoId] || {};
    return Object.values(perLoco).filter(w => w.paneSlug === slug);
  }

  function collectAllWrites(locoId) {
    return Object.values(dirtyByLoco[locoId] || {});
  }

  function collectReadsForPane(pane) {
    // Every row on the pane that has a CV number and isn't a
    // write-only variable (write-only rows don't render a Read button,
    // so we mirror that here). Duplicate CVs are deduped server-side.
    // Variable rows in the rich pane layout are widget divs; speed
    // table rows are ``<tr>``. Both carry ``data-cv``. Raw CV rows
    // (data-cv-name) don't participate in the batch read — they have
    // their own per-row Read button.
    const rows = pane.querySelectorAll("[data-cv]:not([data-cv-name])");
    const reads = [];
    rows.forEach(row => {
      // Rows are included if they either expose a per-row Read button
      // (widget divs, speed-table steps) or are fnmap matrix cells
      // (no per-cell button, but still part of the pane's readable
      // surface).
      const hasReadBtn = row.querySelector(".row-read-btn") !== null;
      const isFnmapCell = row.classList.contains("fnmap-cell");
      if (!hasReadBtn && !isFnmapCell) return;
      const cv = parseInt(row.dataset.cv, 10);
      if (!Number.isFinite(cv)) return;
      reads.push({cv, item: row.dataset.item || ""});
    });
    return reads;
  }

  function readProgrammingMode() {
    // Two-radio picker at the page level. Returns
    //   { mode: "service" | "ops", address, isLongAddress }
    // Defaults to service if the picker isn't in the DOM (backwards
    // compat with test pages that skip the loco page shell).
    const picker = document.querySelector(".programming-mode-picker");
    if (!picker) return {mode: "service"};
    const checked = picker.querySelector(
      'input[name="programming-mode"]:checked',
    );
    const mode = checked ? checked.value : "service";
    if (mode !== "ops") return {mode: "service"};
    const address = parseInt(picker.dataset.locoAddress, 10);
    const isLongAddress = picker.dataset.longAddress === "1";
    return {mode, address, isLongAddress};
  }

  function expandCompositeIntoSubWrites(entry) {
    // Composite dirty entries need to be expanded into per-sub-variable
    // writes on the way out — the server's write callback looks up
    // each sub-variable by ``item`` name and RMWs / bank-sets as
    // needed. ``cv: 0`` is a sentinel meaning "resolve from item".
    const settings = entry.compositeSettings || [];
    return settings.map(s => ({
      cv: 0, value: parseInt(s.value, 10), item: s.item,
    }));
  }

  function toRequestPayload(writes, pane) {
    const modeInfo = readProgrammingMode();
    // Expand composite entries into their sub-variable writes so the
    // server sees a flat list of {cv, value, item} — every entry
    // resolvable via the existing per-variable write callback.
    const flat = [];
    for (const w of writes) {
      if (w.kind === "composite") {
        for (const sub of expandCompositeIntoSubWrites(w)) {
          flat.push(sub);
        }
      } else {
        flat.push({cv: w.cv, value: w.value, item: w.item});
      }
    }
    const payload = {
      writes: flat,
      mode: modeInfo.mode,
      fileName: fileName(pane) || undefined,
    };
    if (modeInfo.mode === "ops") {
      payload.address = modeInfo.address;
      payload.isLongAddress = modeInfo.isLongAddress;
    }
    return payload;
  }

  async function startBatch(pane, writes) {
    if (!writes.length) return;
    const locoId = locoIdFromPane(pane);
    showProgress(pane, `Queuing ${writes.length} write${writes.length === 1 ? "" : "s"}…`);
    setFill(pane, 0);

    let r;
    try {
      r = await fetch(`/programmer/loco/${encodeURIComponent(locoId)}/write-batch`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(toRequestPayload(writes, pane)),
      });
    } catch (e) {
      setStatus(pane, `Network error: ${e.message}`);
      return;
    }
    if (r.status === 409) {
      setStatus(pane, "A batch is already in flight for this loco — cancel it first.");
      return;
    }
    if (!r.ok) {
      const body = await r.text();
      setStatus(pane, `Failed to start batch: ${body || r.status}`);
      return;
    }
    pollStatus(pane, {kind: "write"});
  }

  async function startReadBatch(pane, reads) {
    if (!reads.length) return;
    const locoId = locoIdFromPane(pane);
    showProgress(pane, `Queuing ${reads.length} read${reads.length === 1 ? "" : "s"}…`);
    setFill(pane, 0);
    const modeInfo = readProgrammingMode();
    const payload = {
      reads,
      mode: modeInfo.mode,
      fileName: fileName(pane) || undefined,
    };
    if (modeInfo.mode === "ops") {
      payload.address = modeInfo.address;
      payload.isLongAddress = modeInfo.isLongAddress;
    }
    let r;
    try {
      r = await fetch(`/programmer/loco/${encodeURIComponent(locoId)}/read-batch`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
    } catch (e) {
      setStatus(pane, `Network error: ${e.message}`);
      return;
    }
    if (r.status === 409) {
      setStatus(pane, "A batch is already in flight for this loco — cancel it first.");
      return;
    }
    if (!r.ok) {
      const body = await r.text();
      setStatus(pane, `Failed to start read batch: ${body || r.status}`);
      return;
    }
    pollStatus(pane, {kind: "read"});
  }

  function pollStatus(pane, opts) {
    const kind = (opts && opts.kind) || "write";
    const verb = kind === "read" ? "Reading" : "Writing";
    const pastVerb = kind === "read" ? "read" : "written";
    const locoId = locoIdFromPane(pane);
    stopPolling();
    const tick = async () => {
      let r;
      try {
        r = await fetch(`/programmer/loco/${encodeURIComponent(locoId)}/batch/status`);
      } catch (e) {
        setStatus(pane, `Poll error: ${e.message}`);
        pollTimer = setTimeout(tick, 750);
        return;
      }
      if (r.status === 404) {
        // Nothing in flight — someone cleared, we're done.
        hideProgress(pane);
        return;
      }
      if (!r.ok) {
        setStatus(pane, `Poll failed: ${r.status}`);
        pollTimer = setTimeout(tick, 750);
        return;
      }
      const s = await r.json();
      const pct = s.total ? Math.round(100 * s.completed / s.total) : 0;
      setFill(pane, pct);
      if (s.state === "in_progress") {
        const label = s.in_flight ? (s.in_flight.item || `CV ${s.in_flight.cv}`) : "…";
        setStatus(pane, `${verb} ${s.completed + 1} / ${s.total}: ${label}`);
        pollTimer = setTimeout(tick, 250);
        return;
      }
      // Terminal state.
      const done = s.writes.filter(w => w.result === "done");
      const err  = s.writes.filter(w => w.result === "error");
      const cxl  = s.writes.filter(w => w.result === "cancelled");
      let msg = `${done.length} / ${s.total} ${pastVerb}`;
      if (err.length)  msg += ` · ${err.length} error${err.length === 1 ? "" : "s"}`;
      if (cxl.length)  msg += ` · ${cxl.length} cancelled`;
      setStatus(pane, msg);
      if (kind === "read") {
        // Batch reads updated the CV state store; the visible rows are
        // now stale (esp. masked-bit rows sharing a CV). Refresh the
        // pane via HTMX so every row picks up freshly-extracted values.
        refreshPaneAfterReadBatch(pane);
      } else {
        applyBatchResults(pane, s);
      }
      // Clear so the next start isn't rejected as in-flight.
      try {
        await fetch(`/programmer/loco/${encodeURIComponent(locoId)}/batch/clear`, {method: "POST"});
      } catch (_) { /* ignore */ }
      // Leave the status text visible; hide the cancel button.
      const cancelBtn = pane.querySelector(".pane-cancel-btn");
      if (cancelBtn) cancelBtn.hidden = true;
    };
    pollTimer = setTimeout(tick, 100);
  }

  function refreshPaneAfterReadBatch(pane) {
    const locoId = locoIdFromPane(pane);
    const slug = paneSlug(pane);
    if (!locoId || !slug) return;
    // Fire the same GET the tab click uses. HTMX picks up the swap
    // because #pane-content is the hx-target.
    const target = document.getElementById("pane-content");
    if (!target || !window.htmx) return;
    // Cache-buster: the URL itself is idempotent, so browsers (and any
    // intermediate proxy) will happily serve the stale pre-read fragment
    // if we hit the same path twice. A distinct query string per call
    // forces a fresh render so the newly-read CV state actually shows.
    const bust = `?_=${Date.now()}`;
    window.htmx.ajax(
      "GET",
      `/programmer/loco/${encodeURIComponent(locoId)}/pane/${encodeURIComponent(slug)}${bust}`,
      {target: "#pane-content", swap: "innerHTML"},
    );
  }

  // On terminal, drop dirty flags for rows that wrote successfully so
  // the counter resets and the row visually settles. Rows still in
  // error / cancelled remain dirty for the retry.
  function applyBatchResults(pane, snapshot) {
    const locoId = locoIdFromPane(pane);
    const perLoco = dirtyByLoco[locoId] || {};
    for (const w of snapshot.writes) {
      if (w.result !== "done") continue;
      // Find matching entry by item (preferred) or cv.
      const key = Object.keys(perLoco).find(k =>
        perLoco[k].cv === w.cv &&
        (!w.item || perLoco[k].item === w.item),
      );
      if (key) delete perLoco[key];
    }
    // Also refresh row DOM: matching rows on this pane should update
    // their data-current + drop the dirty class. Widget divs (rich
    // layout), fnmap cells, and speed-table ``<tr>`` all carry
    // data-cv. Multiple rows can share a CV (fnmap bit variables all
    // sit on the same byte), so we match on the write's item first.
    for (const w of snapshot.writes) {
      if (w.result !== "done") continue;
      let row = null;
      if (w.item) {
        row = pane.querySelector(`[data-item="${CSS.escape(w.item)}"]`);
      }
      if (!row) {
        row = pane.querySelector(`[data-cv="${w.cv}"]:not([data-cv-name])`);
      }
      if (!row) continue;
      row.dataset.current = String(w.value);
      const storedCell = row.querySelector('[data-role="stored"]');
      if (storedCell) storedCell.textContent = String(w.value);
      row.classList.remove("dirty");
    }
    refreshDirtyCounter(pane);
  }

  async function cancelBatch(pane) {
    const locoId = locoIdFromPane(pane);
    setStatus(pane, "Cancelling after current CV write…");
    try {
      await fetch(`/programmer/loco/${encodeURIComponent(locoId)}/batch/cancel`, {method: "POST"});
    } catch (e) {
      setStatus(pane, `Cancel failed: ${e.message}`);
    }
    // The poller keeps ticking and will notice CANCELLED terminal state.
  }

  // ------------------------------------------------------------------
  // Event wiring
  // ------------------------------------------------------------------

  // ------------------------------------------------------------------
  // Per-row Read button (JS-driven; HTMX row-swap would replace the
  // <tr> with a <div class="card"> and destroy the row layout).
  // ------------------------------------------------------------------

  async function handleRowWrite(row) {
    // Row-scoped Write: build a single-op batch from this row's
    // current input value and push it through the same queue Write
    // pane uses. Reuses ``startBatch`` so progress + cancel + RMW +
    // indexed-CV setup all apply. Composite rows fan out to their
    // sub-settings via the standard toRequestPayload expansion.
    const pane = row.closest(".programmer-pane");
    if (!pane) return;
    const item = row.dataset.item;
    if (!item) return;
    const input = row.querySelector(".cv-value-input");
    if (!input) return;
    const kind = row.dataset.kind || "";
    const cv = parseInt(row.dataset.cv, 10);
    const entry = {
      item, cv: Number.isFinite(cv) ? cv : 0,
      value: readInputValue(input),
      paneSlug: paneSlug(pane),
      kind,
    };
    if (kind === "composite" && input.tagName === "SELECT") {
      const opt = input.options[input.selectedIndex];
      try {
        entry.compositeSettings = opt && opt.dataset.settings
          ? JSON.parse(opt.dataset.settings)
          : [];
      } catch (_) {
        entry.compositeSettings = [];
      }
    }
    startBatch(pane, [entry]);
  }

  async function handleRowRead(row) {
    const pane = row.closest(".programmer-pane");
    if (!pane) return;
    const locoId = locoIdFromPane(pane);
    const cv = parseInt(row.dataset.cv, 10);
    if (!Number.isFinite(cv)) return;
    const status = row.querySelector('[data-role="row-read-status"]');
    if (status) { status.textContent = "reading…"; status.className = "row-read-status muted small"; }

    const modeInfo = readProgrammingMode();
    const body = {
      cv,
      item: row.dataset.item || undefined,
      mode: modeInfo.mode,
      fileName: fileName(pane) || undefined,
    };
    if (modeInfo.mode === "ops") {
      body.address = modeInfo.address;
      body.isLongAddress = modeInfo.isLongAddress;
    }
    let r;
    try {
      r = await fetch(`/programmer/loco/${encodeURIComponent(locoId)}/read`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
    } catch (e) {
      if (status) { status.textContent = `network: ${e.message}`; status.className = "row-read-status err small"; }
      return;
    }
    if (!r.ok) {
      const text = await r.text();
      if (status) { status.textContent = text || `HTTP ${r.status}`; status.className = "row-read-status err small"; }
      return;
    }
    const data = await r.json();
    // The "human" value is the row-scoped baseline: for masked/enum
    // variables sharing a CV byte, it's the extracted bits; for a
    // plain full-byte variable it equals the wire byte. Fall back to
    // the wire byte if the server didn't compute a human value.
    const humanValue = (data.humanValue !== undefined && data.humanValue !== null)
      ? data.humanValue
      : data.value;
    // Update the stored cell and the row's dirty-tracking baseline.
    row.dataset.current = String(humanValue);
    const storedCell = row.querySelector('[data-role="stored"]');
    if (storedCell) storedCell.textContent = String(humanValue);
    // If the user hasn't dirtied the field yet, sync the input to the
    // freshly-read value. For SELECT rows the value must be the enum's
    // wire value (which equals humanValue for a masked enum). For
    // number inputs the same value works. If they have dirty edits,
    // leave those in place so the user's pending change survives.
    if (!row.classList.contains("dirty")) {
      const input = row.querySelector(".cv-value-input");
      if (input) {
        if (input.type === "checkbox") {
          input.checked = !!humanValue;
        } else {
          input.value = String(humanValue);
        }
      }
    }
    if (status) {
      status.textContent = `read ${humanValue}`;
      status.className = "row-read-status ok small";
    }
    // Re-evaluate dirty state against the new baseline.
    updateRowDirtyState(row);
  }

  // ------------------------------------------------------------------
  // Raw CV table (CVs tab) — per-row read/write + Add CV control.
  // Supports plain CV numbers (7) and indexed compound names
  // (16.1.275). The server does the CV31/CV32 setup for compound.
  // ------------------------------------------------------------------

  async function handleRawCvRead(row) {
    const pane = row.closest(".programmer-pane");
    if (!pane) return;
    const locoId = locoIdFromPane(pane);
    const cvName = row.dataset.cvName;
    if (!cvName) return;
    const status = row.querySelector('[data-role="raw-cv-status"]');
    if (status) { status.textContent = "reading…"; status.className = "raw-cv-status muted small"; }
    const modeInfo = readProgrammingMode();
    const body = {
      cvName, mode: modeInfo.mode,
      fileName: fileName(pane) || undefined,
    };
    if (modeInfo.mode === "ops") {
      body.address = modeInfo.address;
      body.isLongAddress = modeInfo.isLongAddress;
    }
    let r;
    try {
      r = await fetch(`/programmer/loco/${encodeURIComponent(locoId)}/cv/read`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
    } catch (e) {
      if (status) { status.textContent = `network: ${e.message}`; status.className = "raw-cv-status err small"; }
      return;
    }
    if (!r.ok) {
      const text = await r.text();
      if (status) { status.textContent = text || `HTTP ${r.status}`; status.className = "raw-cv-status err small"; }
      return;
    }
    let readValue = 0;
    try {
      const data = await r.json();
      if (typeof data.value === "number") readValue = data.value;
    } catch (_) { /* empty body — leave readValue = 0 */ }
    const stored = row.querySelector('[data-role="stored"]');
    if (stored) {
      const hex = readValue.toString(16).toUpperCase().padStart(2, "0");
      stored.innerHTML = `${readValue} <span class="muted">(0x${hex})</span>`;
    }
    const input = row.querySelector(".raw-cv-value-input");
    if (input) input.value = String(readValue);
    // Fresh read means the input matches stored — not dirty.
    row.classList.remove("raw-cv-dirty");
    if (status) { status.textContent = `read ${readValue}`; status.className = "raw-cv-status ok small"; }
  }

  async function handleRawCvWrite(row) {
    const pane = row.closest(".programmer-pane");
    if (!pane) return;
    const locoId = locoIdFromPane(pane);
    const cvName = row.dataset.cvName;
    if (!cvName) return;
    const input = row.querySelector(".raw-cv-value-input");
    if (!input) return;
    const value = parseInt(input.value, 10);
    if (!Number.isFinite(value) || value < 0 || value > 255) {
      alert("Value must be an integer 0..255");
      return;
    }
    const status = row.querySelector('[data-role="raw-cv-status"]');
    if (status) { status.textContent = "writing…"; status.className = "raw-cv-status muted small"; }
    const modeInfo = readProgrammingMode();
    const body = {
      cvName, value, mode: modeInfo.mode,
      fileName: fileName(pane) || undefined,
    };
    if (modeInfo.mode === "ops") {
      body.address = modeInfo.address;
      body.isLongAddress = modeInfo.isLongAddress;
    }
    let r;
    try {
      r = await fetch(`/programmer/loco/${encodeURIComponent(locoId)}/cv/write`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
    } catch (e) {
      if (status) { status.textContent = `network: ${e.message}`; status.className = "raw-cv-status err small"; }
      return;
    }
    if (!r.ok) {
      const text = await r.text();
      if (status) { status.textContent = text || `HTTP ${r.status}`; status.className = "raw-cv-status err small"; }
      return;
    }
    // The write succeeded on the wire; use the value the user asked
    // for (not the server's echo) to update the stored cell so the
    // UI reflects the write immediately, even if the response body
    // is empty / malformed / non-JSON. Defensive: reading data.value
    // conditionally so a change to the server response shape doesn't
    // silently break the UI update.
    let confirmed = value;
    try {
      const data = await r.json();
      if (typeof data.value === "number") confirmed = data.value;
    } catch (_) { /* empty body or non-JSON — fall back to input value */ }
    const stored = row.querySelector('[data-role="stored"]');
    if (stored) {
      const hex = confirmed.toString(16).toUpperCase().padStart(2, "0");
      stored.innerHTML = `${confirmed} <span class="muted">(0x${hex})</span>`;
    }
    // Clear the "user-edited" tint — the write has committed the
    // value they typed, so the row's no longer diverging from stored.
    row.classList.remove("raw-cv-dirty");
    if (status) { status.textContent = `wrote ${confirmed}`; status.className = "raw-cv-status ok small"; }
  }

  function addRawCvRow(pane) {
    const input = pane.querySelector("#cv-add-input");
    const tbody = pane.querySelector("#raw-cv-tbody");
    if (!input || !tbody) return;
    const name = input.value.trim();
    if (!name) return;
    // Don't duplicate — if a row already exists for this CV name,
    // scroll to it and focus the value input. Iterate manually rather
    // than using ``querySelector`` with an attribute value that would
    // need CSS-escaping for dotted compound names (which some browser
    // versions handle inconsistently).
    for (const tr of tbody.querySelectorAll("tr")) {
      if (tr.dataset.cvName === name) {
        tr.scrollIntoView({behavior: "smooth", block: "center"});
        const valInput = tr.querySelector(".raw-cv-value-input");
        if (valInput) valInput.focus();
        input.value = "";
        return;
      }
    }
    const tr = document.createElement("tr");
    tr.dataset.cvName = name;
    tr.innerHTML = `
      <td class="mono">${name}</td>
      <td class="mono" data-role="stored"><span class="muted">—</span></td>
      <td><input type="number" class="raw-cv-value-input" min="0" max="255" value="0" /></td>
      <td class="row" style="gap:0.25rem">
        <button type="button" class="raw-cv-read-btn">Read</button>
        <button type="button" class="raw-cv-write-btn">Write</button>
        <span class="raw-cv-status muted small" data-role="raw-cv-status"></span>
      </td>`;
    tbody.appendChild(tr);
    wireRawCvRow(tr);
    input.value = "";
    input.focus();
  }

  function wireRawCvRow(row) {
    // Attach direct click listeners per row so the Read/Write button
    // clicks don't rely on the delegated handler alone. Marks the row
    // as dirty (yellow tint) when the value input changes, so the
    // user gets visual confirmation that their edit was recognized.
    if (!row || row._rawCvWired) return;
    row._rawCvWired = true;
    const readBtn = row.querySelector(".raw-cv-read-btn");
    if (readBtn) readBtn.addEventListener("click", () => handleRawCvRead(row));
    const writeBtn = row.querySelector(".raw-cv-write-btn");
    if (writeBtn) writeBtn.addEventListener("click", () => handleRawCvWrite(row));
    const input = row.querySelector(".raw-cv-value-input");
    if (input) {
      const flagDirty = () => row.classList.add("raw-cv-dirty");
      input.addEventListener("input", flagDirty);
      input.addEventListener("change", flagDirty);
    }
  }

  function wireAllRawCvRows(pane) {
    pane.querySelectorAll("tr[data-cv-name]").forEach(wireRawCvRow);
  }

  function wirePane(pane) {
    if (!pane || pane._batchWired) return;
    pane._batchWired = true;

    // Wire every raw-CV row that's currently present. Rows added via
    // "Add CV" later get wired at add-time (see addRawCvRow).
    wireAllRawCvRows(pane);

    // Delegated fallback for the Add-row button (also wired directly
    // below) — belt & braces. Also handles Read/Write clicks on any
    // row that somehow escaped ``wireRawCvRow``.
    pane.addEventListener("click", (evt) => {
      const readBtn = evt.target.closest(".raw-cv-read-btn");
      if (readBtn) {
        const row = readBtn.closest("tr");
        if (row) handleRawCvRead(row);
        return;
      }
      const writeBtn = evt.target.closest(".raw-cv-write-btn");
      if (writeBtn) {
        const row = writeBtn.closest("tr");
        if (row) handleRawCvWrite(row);
        return;
      }
      const addBtn = evt.target.closest("#cv-add-btn");
      if (addBtn) addRawCvRow(pane);
    });

    // Track edits. ``[data-item]`` matches both widget divs (rich
    // layout) and speed-table ``<tr>``; raw CV rows carry
    // ``data-cv-name`` instead and are wired separately.
    pane.addEventListener("input", (evt) => {
      const input = evt.target.closest(".cv-value-input");
      if (!input) return;
      const row = input.closest("[data-item]");
      if (row) updateRowDirtyState(row);
    });
    pane.addEventListener("change", (evt) => {
      const input = evt.target.closest(".cv-value-input");
      if (!input) return;
      const row = input.closest("[data-item]");
      if (row) updateRowDirtyState(row);
    });

    // Per-row Read / Write buttons — delegated so re-rendered rows
    // still work. Row-scoped Write sends just this variable's current
    // input value as a single-op batch through the same queue Write
    // pane uses (RMW, indexed-CV setup, composite fan-out all Just
    // Work because the write path is unified).
    pane.addEventListener("click", (evt) => {
      const readBtn = evt.target.closest(".row-read-btn");
      if (readBtn) {
        const row = readBtn.closest("[data-item]");
        if (row) handleRowRead(row);
        return;
      }
      const writeBtn = evt.target.closest(".row-write-btn");
      if (writeBtn) {
        const row = writeBtn.closest("[data-item]");
        if (row) handleRowWrite(row);
      }
    });

    // Buttons.
    const addCvBtn = pane.querySelector("#cv-add-btn");
    if (addCvBtn) addCvBtn.addEventListener("click", () => addRawCvRow(pane));
    const addCvInput = pane.querySelector("#cv-add-input");
    if (addCvInput) addCvInput.addEventListener("keydown", (evt) => {
      if (evt.key === "Enter") { evt.preventDefault(); addRawCvRow(pane); }
    });

    const readPaneBtn = pane.querySelector(".pane-read-btn");
    if (readPaneBtn) readPaneBtn.addEventListener("click", () => {
      startReadBatch(pane, collectReadsForPane(pane));
    });
    const paneBtn = pane.querySelector('.pane-write-btn[data-scope="pane"]');
    if (paneBtn) paneBtn.addEventListener("click", () => {
      startBatch(pane, collectWritesForPane(pane));
    });
    const allBtn = pane.querySelector('.pane-write-btn[data-scope="all"]');
    if (allBtn) allBtn.addEventListener("click", () => {
      startBatch(pane, collectAllWrites(locoIdFromPane(pane)));
    });
    const cancelBtn = pane.querySelector(".pane-cancel-btn");
    if (cancelBtn) cancelBtn.addEventListener("click", () => cancelBatch(pane));

    rehydrateDirty(pane);
  }

  // First page load.
  document.addEventListener("DOMContentLoaded", () => {
    wirePane(paneRoot());
  });

  // HTMX pane swap.
  document.addEventListener("htmx:afterSwap", (evt) => {
    const target = evt.detail && evt.detail.target;
    if (!target) return;
    // Rewire whichever new pane got swapped in.
    const pane = target.classList && target.classList.contains("programmer-pane")
      ? target
      : target.querySelector && target.querySelector(".programmer-pane");
    if (pane) wirePane(pane);
  });
})();
