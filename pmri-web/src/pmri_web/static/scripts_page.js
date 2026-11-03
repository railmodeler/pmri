/**
 * Persist textarea content on the /scripts page across navigations.
 *
 * The REPL history is server-side and re-renders on page load, but
 * *unsent* textarea content — mid-typed REPL commands, in-progress
 * batch scripts — lives in the DOM and vanishes when the browser
 * navigates away. That breaks the "let me check the turnouts page
 * for a name and come back" workflow.
 *
 * Storage strategy:
 * - Two keys, one per textarea (REPL vs batch runner).
 * - Saved on every ``input`` event (cheap, batched by the browser).
 * - Restored on ``DOMContentLoaded``.
 * - Cleared on successful submission — hooked via htmx's
 *   ``htmx:afterOnLoad`` event so we know the server accepted it.
 *
 * ``localStorage`` is per-origin, so multiple JMRI installs on
 * the same host share it. Not a concern on the single-Pi
 * deployment; a multi-install setup could namespace by pathname
 * if it ever mattered.
 */
(function () {
  const KEYS = {
    repl: "jmri-scripts-repl-draft",
    batch: "jmri-scripts-batch-draft",
    batchName: "jmri-scripts-batch-name-draft",
    replHistory: "jmri-scripts-repl-history",
  };

  // Cap the client-side history at 100 entries — matches the
  // server-side ReplSession cap and keeps localStorage bounded
  // (100 entries × ~200 bytes typical = ~20 KB).
  const HISTORY_MAX = 100;

  function loadHistory() {
    try {
      const raw = localStorage.getItem(KEYS.replHistory);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  }

  function saveHistory(entries) {
    try {
      localStorage.setItem(KEYS.replHistory, JSON.stringify(entries));
    } catch (e) {
      // Storage full or disabled — silent degrade.
    }
  }

  function pushHistory(source) {
    if (!source || !source.trim()) return;
    const entries = loadHistory();
    // Deduplicate against the most recent entry only — bash-style;
    // repeated Enter on the same command doesn't spam the history.
    if (entries.length > 0 && entries[entries.length - 1] === source) {
      return;
    }
    entries.push(source);
    while (entries.length > HISTORY_MAX) {
      entries.shift();
    }
    saveHistory(entries);
  }

  /**
   * Find the REPL textarea. It's the one inside the
   * "Interactive Python (REPL)" card's form posting to /scripts/repl.
   */
  function replTextarea() {
    const form = document.querySelector('form[hx-post="/scripts/repl"]');
    return form ? form.querySelector('textarea[name="source"]') : null;
  }

  /**
   * Find the batch runner textarea. Same pattern, different endpoint.
   */
  function batchTextarea() {
    const form = document.querySelector('form[hx-post="/scripts/run"]');
    return form ? form.querySelector('textarea[name="source"]') : null;
  }

  function batchNameInput() {
    const form = document.querySelector('form[hx-post="/scripts/run"]');
    return form ? form.querySelector('input[name="name"]') : null;
  }

  function saveDraft(key, value) {
    try {
      if (value) {
        localStorage.setItem(key, value);
      } else {
        localStorage.removeItem(key);
      }
    } catch (e) {
      // Storage disabled or full — graceful degradation, drafts
      // just don't persist. Not worth surfacing.
    }
  }

  function restoreDraft(key) {
    try {
      return localStorage.getItem(key) || "";
    } catch (e) {
      return "";
    }
  }

  function wire(element, storageKey) {
    if (!element) return;
    // Restore first — the input handler below shouldn't fire and
    // wipe out the stored value with the empty initial content.
    const stored = restoreDraft(storageKey);
    if (stored && !element.value) {
      element.value = stored;
    }
    element.addEventListener("input", function () {
      saveDraft(storageKey, element.value);
    });
  }

  /**
   * Return true iff the caret is on the first visual line of the
   * textarea. We use this to decide whether Up recalls history
   * (caret at first line — no more room to move up) vs moves the
   * caret within the text (there IS a line above).
   *
   * Logic: no newlines in the text before the caret means we're
   * on line 1.
   */
  function caretOnFirstLine(el) {
    const pos = el.selectionStart;
    if (pos === null || pos === undefined) return true;
    return el.value.slice(0, pos).indexOf("\n") === -1;
  }

  function caretOnLastLine(el) {
    const pos = el.selectionEnd;
    if (pos === null || pos === undefined) return true;
    return el.value.slice(pos).indexOf("\n") === -1;
  }

  /**
   * Attach Up/Down history recall to the REPL textarea. Mirrors
   * the CPython REPL / bash conventions:
   *
   * - **Up** at first line: replace textarea with the previous
   *   history entry. Repeated Up keeps going backwards.
   * - **Down** at last line: forward. Past the newest entry we
   *   restore whatever the user was typing before they started
   *   navigating (their "draft").
   * - Any other key press cancels history navigation — the next
   *   Up starts from the newest entry again.
   *
   * Only intercepts when the caret is at a boundary line so
   * multi-line editing still works normally.
   */
  function wireHistoryNav(el) {
    if (!el) return;
    // ``historyIndex``: null = editing current draft; 0..N-1 =
    // index INTO the history array where the newest entry is at
    // the end. Walking Up decrements toward 0 (older); Down
    // increments toward null (draft).
    let historyIndex = null;
    let savedDraft = "";

    function beginNav() {
      if (historyIndex === null) {
        savedDraft = el.value;
      }
    }

    function reset() {
      historyIndex = null;
      savedDraft = "";
    }

    el.addEventListener("keydown", function (evt) {
      // Modifiers other than "no modifier" always fall through
      // (Ctrl+Enter submits; Shift+Up selects; etc.).
      if (evt.altKey || evt.ctrlKey || evt.metaKey || evt.shiftKey) {
        if (evt.key !== "Enter") reset();
        return;
      }

      if (evt.key === "ArrowUp") {
        if (!caretOnFirstLine(el)) return;
        const entries = loadHistory();
        if (entries.length === 0) return;
        beginNav();
        if (historyIndex === null) {
          historyIndex = entries.length - 1;
        } else if (historyIndex > 0) {
          historyIndex -= 1;
        } else {
          return;  // already at the oldest
        }
        evt.preventDefault();
        el.value = entries[historyIndex];
        // Move caret to end so a subsequent Up keeps recalling.
        el.selectionStart = el.selectionEnd = el.value.length;
        saveDraft(KEYS.repl, el.value);
        return;
      }

      if (evt.key === "ArrowDown") {
        if (!caretOnLastLine(el)) return;
        if (historyIndex === null) return;
        const entries = loadHistory();
        if (historyIndex < entries.length - 1) {
          historyIndex += 1;
          evt.preventDefault();
          el.value = entries[historyIndex];
          el.selectionStart = el.selectionEnd = el.value.length;
          saveDraft(KEYS.repl, el.value);
        } else {
          // Past the newest entry — restore the pre-navigation
          // draft. Mirrors bash's Down-past-end-of-history
          // behavior.
          historyIndex = null;
          evt.preventDefault();
          el.value = savedDraft;
          el.selectionStart = el.selectionEnd = el.value.length;
          saveDraft(KEYS.repl, el.value);
        }
        return;
      }

      // Any other key while navigating: commit to editing this
      // entry as a new draft. The user is presumably about to
      // modify it before re-submitting.
      if (historyIndex !== null && evt.key !== "Enter") {
        reset();
      }
    });

    // Successful submit resets the navigation state so the next
    // Up starts fresh at the newest.
    document.body.addEventListener("htmx:afterOnLoad", function (evt) {
      const target = evt.detail && evt.detail.elt;
      if (target && target.getAttribute && target.getAttribute("hx-post") === "/scripts/repl") {
        if (evt.detail.xhr && evt.detail.xhr.status < 400) {
          reset();
        }
      }
    });
  }

  function clearOnSubmitSuccess() {
    // Hook htmx's after-load event. If the request came from the
    // REPL or batch form and got a 2xx, clear that form's draft.
    document.body.addEventListener("htmx:afterOnLoad", function (evt) {
      const target = evt.detail && evt.detail.elt;
      if (!target || !target.getAttribute) return;
      const url = target.getAttribute("hx-post");
      if (url === "/scripts/repl") {
        // REPL: clear the box on successful submit — mirrors the
        // Python REPL where the input line clears after Enter.
        // Only clear when the submission was actually accepted
        // (2xx); a 400/500 leaves the text so the user can retry.
        if (evt.detail.xhr && evt.detail.xhr.status < 400) {
          const ta = replTextarea();
          if (ta) {
            // Record what was submitted BEFORE clearing, so
            // history recall (Up/Down arrows) can replay it.
            // Read from the form-encoded body rather than the
            // now-cleared textarea; htmx stashes the request
            // parameters on the event detail.
            let submitted = "";
            if (evt.detail.requestConfig && evt.detail.requestConfig.parameters) {
              submitted = evt.detail.requestConfig.parameters.source || "";
            }
            if (!submitted) submitted = ta.value;
            pushHistory(submitted);
            ta.value = "";
            saveDraft(KEYS.repl, "");
          }
        }
      } else if (url === "/scripts/run") {
        if (evt.detail.xhr && evt.detail.xhr.status < 400) {
          const ta = batchTextarea();
          const name = batchNameInput();
          if (ta) { ta.value = ""; saveDraft(KEYS.batch, ""); }
          if (name) { name.value = ""; saveDraft(KEYS.batchName, ""); }
        }
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wire(replTextarea(), KEYS.repl);
    wire(batchTextarea(), KEYS.batch);
    wire(batchNameInput(), KEYS.batchName);
    wireHistoryNav(replTextarea());
    clearOnSubmitSuccess();
  });
})();
