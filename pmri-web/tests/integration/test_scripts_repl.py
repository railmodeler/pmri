"""Web-side REPL flow smoke tests + regression for the "print
disappears" symptom from the beta 3 report.
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_repl_submit_shows_output(client: TestClient) -> None:
    """A submit of ``print('hi')`` renders the fragment with the
    output visible -- not the "output disappears" symptom the beta 3
    tester reported for the /scripts/run path."""
    r = client.post("/scripts/repl", data={"source": "print('hi')"})
    assert r.status_code == 200
    assert "hi" in r.text
    # Prompt marker present + entry rendered.
    assert "&gt;&gt;&gt;" in r.text or ">>>" in r.text


def test_repl_variable_persists_across_submissions(
    client: TestClient,
) -> None:
    """The REPL's raison d'être. Two POSTs, variable set in the
    first is visible in the second."""
    client.post("/scripts/repl", data={"source": "x = 42"})
    r = client.post("/scripts/repl", data={"source": "x * 2"})
    assert "84" in r.text


def test_repl_expression_shows_repr(client: TestClient) -> None:
    """Bare expression echoes its ``repr`` (Jupyter-style)."""
    r = client.post("/scripts/repl", data={"source": "2 + 2"})
    assert "4" in r.text


def test_repl_exception_renders_traceback(client: TestClient) -> None:
    r = client.post("/scripts/repl", data={"source": "1 / 0"})
    assert "ZeroDivisionError" in r.text
    assert "Traceback" in r.text


def test_repl_reset_clears_history(client: TestClient) -> None:
    client.post("/scripts/repl", data={"source": "print('one')"})
    client.post("/scripts/repl", data={"source": "print('two')"})
    r = client.post("/scripts/repl/reset")
    assert "one" not in r.text
    assert "two" not in r.text
    assert "idle" in r.text.lower() or "submit an expression" in r.text.lower()


def test_repl_shim_globals_available_immediately(
    client: TestClient,
) -> None:
    """First submission can touch ``turnouts`` without any import
    -- the shim's globals are pre-seeded on interpreter construction."""
    r = client.post("/scripts/repl", data={
        "source": "type(turnouts).__name__",
    })
    assert "_ManagerShim" in r.text or "ManagerShim" in r.text


def test_scripts_run_still_captures_output_after_repl_added(
    client: TestClient,
) -> None:
    """Regression: the beta 3 tester reported that /scripts/run
    showed no output for ``print('hello')``. With the REPL added
    the batch path is still expected to capture output; this pins
    that contract in one test."""
    r = client.post("/scripts/run", data={
        "name": "print-check",
        "source": "print('hello')",
    })
    assert r.status_code == 200
    # Give the worker thread + buffer flush a beat.
    import time
    for _ in range(20):
        if "hello" in r.text:
            break
        time.sleep(0.05)
        r = client.get("/scripts/refresh")
    assert "hello" in r.text, (
        "print output missing from /scripts/run response -- the "
        "capture stream regressed"
    )
