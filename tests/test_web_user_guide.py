"""T-25: the user and startup guide matches the actually served application.

The README startup commands are checked against the real CLI surface, and the
documented startup path is exercised as an actual process: the page opens and
model-free reads work with no configured model, then the service stops
cleanly. No live model, no network beyond the loopback interface, and no
writes to the working ledger (temporary controlled dataset only).
"""

import json
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from agent_project.web.__main__ import main

REPO_DIR = Path(__file__).resolve().parents[1]
README = (REPO_DIR / "README.md").read_text(encoding="utf-8")

START = "## Run the web application"


def guide_section() -> str:
    """Return the documented web startup guide as a string."""
    start = README.index(START)
    end = README.index("\n## ", start + len(START))
    return README[start:end]


def test_guide_documents_real_command_options_and_stopping():
    section = guide_section()
    # The exact entrypoint, address, alternatives and stop instruction.
    assert "uv run python -m agent_project.web" in section
    assert "uv run python -m agent_project.web --port" in section
    assert "http://127.0.0.1:8765" in section
    assert "127.0.0.1" in section and "loopback" in section
    assert "Ctrl+C" in section


def test_guide_option_names_match_the_cli(capsys):
    documented = set(re.findall(r"--[a-z][a-z-]+", guide_section()))
    assert documented, "the guide must name CLI options"
    with pytest.raises(SystemExit):
        main(["--help"])
    parser_text = capsys.readouterr().out
    for name in documented:
        assert name in parser_text, f"guide option {name} is not a real CLI name"


def test_guide_describes_model_config_consents_limitations_and_p1():
    section = guide_section()
    assert "MODEL_NAME" in section and "LLM_BASE_URL" in section
    assert "without a model" in section
    assert "not reachable from a LAN" in section
    assert "no inference cancellation" in section
    # Restart loses in-memory session, run and pending-decision state.
    assert "restart" in section and "pending" in section
    # P1 stays out of the MVP scope, without claiming implemented features.
    assert "AC-27" in section and "AC-28" in section
    assert "not implemented" in section


def test_readme_documents_test_commands_and_live_adaptation_evidence():
    # Browser and offline test commands remain documented.
    assert "uv run --no-sync pytest tests/test_browser_env.py" in README
    assert "uv run --no-sync pytest -ra" in README
    assert "## Live adaptation evidence" in README
    assert "evidence/trace_multitool_adaptation.jsonl" in README


def free_loopback_port() -> int:
    """Bind once on 127.0.0.1:0 and release the port before the server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_until_ready(url: str, deadline_s: float = 15.0) -> httpx.Response:
    started = time.monotonic()
    last_error: Exception | None = None
    while time.monotonic() - started < deadline_s:
        try:
            response = httpx.get(f"{url}/api/session", timeout=1.0)
            if response.status_code == 200:
                return response
        except httpx.HTTPError as error:  # connection refused while starting
            last_error = error
            time.sleep(0.1)
    raise AssertionError(f"Server did not become ready: {last_error}")


@pytest.fixture
def served_app(controlled_dataset):
    """Run the documented CLI startup against a temporary dataset."""
    data_path, changes_path = controlled_dataset
    port = free_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_project.web",
            "--port",
            str(port),
            "--data-path",
            str(data_path),
            "--changes-path",
            str(changes_path),
        ],
        cwd=str(REPO_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        wait_until_ready(url)
        yield url
    finally:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=15)
        stdout = (process.stdout.read() or b"").decode("utf-8", "replace")
        stderr = (process.stderr.read() or b"").decode("utf-8", "replace")
        assert process.returncode == 0, stderr or stdout
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))  # the port must be released


def test_serving_cli_opens_the_screen_without_a_model(served_app):
    url = served_app
    # Cookies mirror the page bootstrap exactly as the browser would do it.
    with httpx.Client() as browser:
        session = browser.get(f"{url}/api/session", timeout=5.0)
        assert session.status_code == 200
        assert json.loads(session.text)["active_run_id"] is None

        # Opening the screen performs reads only, with no analysis started.
        page = browser.get(f"{url}/", timeout=5.0)
        assert page.status_code == 200
        assert "Household Expense Checker" in page.text

        transactions = browser.get(f"{url}/api/transactions", timeout=5.0)
        assert transactions.status_code == 200
        body = json.loads(transactions.text)
        assert len(body["items"]) == 5
        assert body["possibly_truncated"] is False
