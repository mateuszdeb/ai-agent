"""T-12: start and read run endpoints over the in-process ASGI boundary.

Every scenario exchanges only real HTTP frames: session bootstrap, origin,
CSRF and instance checks belong to the security middleware, so these tests
exercise ownership, deduplication, conflict, restart and validation behavior
without a listening server, a live model or any domain write.
"""

import asyncio
import threading
import time

import httpx
import pytest

from agent_project.web import WebSettings, create_app
from agent_project.web.runner import RunManager
from agent_project.web.runs import TERMINAL_RUN_STATUSES
from agent_project.web.sessions import SESSION_COOKIE

ORIGIN = "http://127.0.0.1:8765"


class BlockingClient:
    """Scripted offline client whose first call stays pending until released."""

    model = "mock"
    provider = "mock"

    def __init__(self, first_answer: str, follow_ups: list[dict] | None = None):
        self.answer = first_answer
        self.responses = follow_ups or []
        self.entered = threading.Event()
        self.gate = threading.Event()
        self.calls: list[int] = []

    def complete(self, messages, tools=None) -> dict:
        self.calls.append(len(messages))
        self.entered.set()
        assert self.gate.wait(15.0), "The blocked call was never released."
        if self.responses:
            return self.responses.pop(0)
        return {"text": self.answer, "tool_calls": []}


@pytest.fixture
def build_app(controlled_dataset, tmp_path):
    """Create a fully offline app; the runner is replaced with a scripted one."""
    data_path, changes_path = controlled_dataset
    apps: list = []

    def build(client):
        app = create_app(
            settings=WebSettings(data_path=data_path, changes_path=changes_path)
        )
        app.state.runner.shutdown(wait=False)
        app.state.runner = RunManager(
            runs=app.state.runs,
            data_path=data_path,
            changes_path=changes_path,
            client=client,
            traces_dir=tmp_path / "traces",
        )
        apps.append(app)
        return app

    def build_scripted(responses):
        from agent_project.llm import MockClient

        # Keep first-response semantics small; success answer only.
        return build(MockClient(list(responses)))

    build.scripted = build_scripted
    yield build
    for app in apps:
        app.state.runner.shutdown(wait=False)


def exercise(app, scenario):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=ORIGIN,
        ) as client:
            try:
                return await scenario(client)
            finally:
                app.state.runner.shutdown(wait=False)

    return asyncio.run(run())


async def bootstrap(client):
    response = await client.get("/api/session")
    assert response.status_code == 200, response.text
    return response.json()


def write_headers(session):
    return {"Origin": ORIGIN, "X-CSRF-Token": session["csrf_token"]}


def start_body(session, *, goal="Review expenses.", request_id="request_1"):
    return {
        "goal": goal,
        "request_id": request_id,
        "instance_id": session["instance_id"],
    }


async def wait_for_terminal(runs, run_id: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = runs.get_run(run_id)
        if run is not None and run.status in TERMINAL_RUN_STATUSES:
            return run
        await asyncio.sleep(0.05)
    pytest.fail("The run never reached a terminal state.")


def assert_safe_headers(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_start_run_returns_202_receipt_and_readable_snapshot(build_app):
    app = build_app.scripted(
        [{"text": "Analysis complete: found duplicate charges.", "tool_calls": []}]
    )

    async def scenario(client):
        session = await bootstrap(client)
        response = await client.post(
            "/api/runs",
            json=start_body(session),
            headers=write_headers(session),
        )
        assert response.status_code == 202, response.text
        receipt = response.json()
        assert set(receipt) == {"run_id", "status", "version"}
        assert receipt["run_id"].startswith("run_")
        assert receipt["status"] == "running"
        assert receipt["version"] == 1
        assert_safe_headers(response)

        # No GET starts or restarts work.
        read_again = await client.get(f"/api/runs/{receipt['run_id']}")
        assert read_again.status_code == 200

        finished = await wait_for_terminal(app.state.runs, receipt["run_id"])
        assert finished.version > 1

        snapshot_response = await client.get(f"/api/runs/{receipt['run_id']}")
        assert snapshot_response.status_code == 200
        snapshot = snapshot_response.json()
        assert snapshot["run_id"] == receipt["run_id"]
        assert snapshot["status"] == "completed"
        assert snapshot["result"] == "Analysis complete: found duplicate charges."
        assert snapshot["goal"] == "Review expenses."
        assert snapshot["observations"] == []
        # Unavailable data is simply omitted, never invented.
        assert "approval_outcome" not in snapshot
        assert "error" not in snapshot
        assert snapshot["version"] == finished.version

        # The answer text is an answer, never an execution claim by itself.
        assert "trace" not in snapshot
        assert "owner" not in snapshot

    exercise(app, scenario)


def test_replay_of_same_request_returns_the_same_run(build_app):
    app = build_app.scripted(
        [{"text": "Analysis complete: found duplicate charges.", "tool_calls": []}]
    )

    async def scenario(client):
        session = await bootstrap(client)
        first = await client.post(
            "/api/runs",
            json=start_body(session),
            headers=write_headers(session),
        )
        second = await client.post(
            "/api/runs",
            json=start_body(session),
            headers=write_headers(session),
        )
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()
        assert app.state.runs.count() == 1

        replay_later = await client.post(
            "/api/runs",
            json=start_body(session),
            headers=write_headers(session),
        )
        assert replay_later.status_code == 202
        replay_later_body = replay_later.json()
        assert replay_later_body["run_id"] == first.json()["run_id"]
        assert app.state.runs.count() == 1

    exercise(app, scenario)


def test_same_request_key_with_different_goal_conflicts(build_app):
    app = build_app.scripted(
        [{"text": "Analysis complete: found duplicate charges.", "tool_calls": []}]
    )

    async def scenario(client):
        session = await bootstrap(client)
        accepted = await client.post(
            "/api/runs",
            json=start_body(session, goal="First goal."),
            headers=write_headers(session),
        )
        assert accepted.status_code == 202, accepted.text

        perverted = await client.post(
            "/api/runs",
            json=start_body(session, goal="Second, different goal."),
            headers=write_headers(session),
        )
        assert perverted.status_code == 409, perverted.text
        assert perverted.json()["error"]["code"] == "request_id_conflict"
        assert app.state.runs.count() == 1

        # The original replay remains intact after the conflict attempt.
        replay = await client.post(
            "/api/runs",
            json=start_body(session, goal="First goal."),
            headers=write_headers(session),
        )
        assert replay.status_code == 202
        assert replay.json()["run_id"] == accepted.json()["run_id"]
        assert app.state.runs.count() == 1

    exercise(app, scenario)


def test_second_run_while_active_conflicts_and_replay_serves_active_run(build_app):
    client_model = BlockingClient("Blocked analysis finished.")
    app = build_app(client_model)

    async def scenario(client):
        session = await bootstrap(client)
        accepted = await client.post(
            "/api/runs",
            json=start_body(session, goal="Blocking goal.", request_id="request_1"),
            headers=write_headers(session),
        )
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["run_id"]
        assert client_model.entered.wait(10.0)

        busy = await client.post(
            "/api/runs",
            json=start_body(session, goal="Different goal.", request_id="request_2"),
            headers=write_headers(session),
        )
        assert busy.status_code == 409, busy.text
        assert busy.json()["error"]["code"] == "run_in_progress"
        assert app.state.runs.count() == 1

        replay = await client.post(
            "/api/runs",
            json=start_body(session, goal="Blocking goal.", request_id="request_1"),
            headers=write_headers(session),
        )
        assert replay.status_code == 202
        assert replay.json()["run_id"] == run_id
        assert app.state.runs.count() == 1

        snapshot = await client.get(f"/api/runs/{run_id}")
        assert snapshot.status_code == 200
        assert snapshot.json()["status"] == "running"

        client_model.gate.set()
        finished = await wait_for_terminal(app.state.runs, run_id)
        assert finished.status == "completed"
        await wait_for_terminal(app.state.runs, run_id)  # keep loop stable

    exercise(app, scenario)
    assert client_model.calls


def test_foreign_and_unknown_runs_are_indistinguishable_safe_404(build_app):
    app = build_app.scripted(
        [{"text": "Analysis complete: found duplicate charges.", "tool_calls": []}]
    )

    async def scenario(client):
        owner_session = await bootstrap(client)
        accepted = await client.post(
            "/api/runs",
            json=start_body(owner_session),
            headers=write_headers(owner_session),
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        owner_cookie = client.cookies.get(SESSION_COOKIE)
        await wait_for_terminal(app.state.runs, run_id)

        missing = await client.get("/api/runs/run_does_not_exist")
        assert missing.status_code == 404, missing.text
        assert missing.json()["error"]["code"] == "run_unavailable"

        # A second anonymous visitor owns nothing.
        client.cookies.clear()
        other_session = await bootstrap(client)
        foreign = await client.get(f"/api/runs/{run_id}")
        assert foreign.status_code == 404, foreign.text
        foreign_body = foreign.text
        missing_again = await client.get("/api/runs/run_does_not_exist")
        assert missing_again.text == foreign_body
        assert other_session["active_run_id"] is None
        assert app.state.runs.count() == 1

        # The original owner still reads the snapshot.
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, owner_cookie)
        owner_read = await client.get(f"/api/runs/{run_id}")
        assert owner_read.status_code == 200
        assert owner_read.json()["status"] == "completed"

    exercise(app, scenario)


def test_restart_requires_new_instance_and_hides_previous_runs(build_app):
    first = build_app.scripted(
        [{"text": "Analysis complete: found duplicate charges.", "tool_calls": []}]
    )
    saved: dict[str, str] = {}

    async def first_scenario(client):
        session = await bootstrap(client)
        response = await client.post(
            "/api/runs",
            json=start_body(session),
            headers=write_headers(session),
        )
        assert response.status_code == 202
        saved["instance"] = session["instance_id"]
        saved["run"] = response.json()["run_id"]
        await wait_for_terminal(first.state.runs, saved["run"])

    exercise(first, first_scenario)

    second = build_app.scripted(
        [{"text": "Fresh analysis finished.", "tool_calls": []}]
    )

    async def second_scenario(client):
        fresh_session = await bootstrap(client)
        assert fresh_session["instance_id"] != saved["instance"]

        stale_instance = await client.post(
            "/api/runs",
            json={
                "goal": "Review expenses.",
                "request_id": "request_1",
                "instance_id": saved["instance"],
            },
            headers=write_headers(fresh_session),
        )
        assert stale_instance.status_code == 409, stale_instance.text
        assert stale_instance.json()["error"]["code"] == "instance_changed"

        lost = await client.get(f"/api/runs/{saved['run']}")
        assert lost.status_code == 404, lost.text
        assert lost.json()["error"]["code"] == "run_unavailable"
        assert second.state.runs.count() == 0

    exercise(second, second_scenario)


def test_validation_errors_run_nothing(build_app):
    from agent_project.llm import MockClient

    class CountingMockClient(MockClient):
        def __init__(self, responses):
            super().__init__(responses)
            self.complete_calls = 0

        def complete(self, messages, tools=None):
            self.complete_calls += 1
            return super().complete(messages, tools)

    offline = CountingMockClient(responses=[])
    app = build_app(offline)

    async def scenario(client):
        session = await bootstrap(client)
        cases = [
            start_body(session, goal="   "),  # whitespace-only goal
            start_body(session, goal="x" * 8001),  # over the advertised bound
            start_body(session, request_id=""),  # blank dedup key
            {"goal": "Goal.", "request_id": "request_1"},  # missing instance_id
            {
                "goal": "Goal.",
                "request_id": "request_1",
                "instance_id": session["instance_id"],
                # Extra client fields must never pass strict validation.
                "confirmed": True,
            },
        ]
        for body in cases:
            response = await client.post(
                "/api/runs", json=body, headers=write_headers(session)
            )
            assert response.status_code == 422, (body, response.text)
            payload = response.json()
            assert payload["error"]["code"] == "invalid_request"
            assert not {"run_id", "status", "version"} & set(payload)
            assert_safe_headers(response)

        unknown = await client.get("/api/runs/run_absent")
        assert unknown.status_code == 404
        assert offline.complete_calls == 0
        assert app.state.runs.count() == 0
        assert app.state.runs.active_count() == 0
        assert app.state.runner.is_busy() is False

    exercise(app, scenario)


def test_get_never_starts_a_new_run(build_app):
    app = build_app.scripted(
        [{"text": "Analysis complete: found duplicate charges.", "tool_calls": []}]
    )

    async def scenario(client):
        session = await bootstrap(client)
        before = (
            app.state.runs.count(),
            app.state.runs.active_count(),
            app.state.runs.terminal_count(),
        )
        for _ in range(3):
            read = await client.get("/api/runs/run_unknown")
            assert read.status_code == 404
            after = (
                app.state.runs.count(),
                app.state.runs.active_count(),
                app.state.runs.terminal_count(),
            )
            assert after == before

        accepted = await client.post(
            "/api/runs",
            json=start_body(session),
            headers=write_headers(session),
        )
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        await wait_for_terminal(app.state.runs, run_id)
        read = await client.get(f"/api/runs/{run_id}")
        assert read.status_code == 200
        assert read.json()["status"] == "completed"
        assert app.state.runs.count() == 1

    exercise(app, scenario)
