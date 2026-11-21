"""Local same-origin app; factory does not start a model or touch domain data."""

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

from agent_project.web.approvals import (
    ApprovalAccessDeniedError,
    ApprovalNotFoundError,
    ApprovalStore,
    PendingAction,
)
from agent_project.web.instance_lock import WebInstanceLock
from agent_project.web.presentation import http_error
from agent_project.web.read_service import (
    ReadUnavailableError,
    TransactionReadService,
    parse_limit,
    parse_read_query,
)
from agent_project.web.runner import RunManager, trace_run_event
from agent_project.web.runs import (
    RequestIdConflictError,
    RunAccessDeniedError,
    RunInProgressError,
    RunStore,
)
from agent_project.web.schemas import (
    ApprovalList,
    DecisionReceipt,
    DecisionRequest,
    RunReceipt,
    RunSnapshot,
    SessionResponse,
    StartRunRequest,
    TransactionDetails,
    TransactionList,
)
from agent_project.web.security import LocalSecurityMiddleware
from agent_project.web.sessions import (
    SESSION_COOKIE,
    SessionCapacityError,
    SessionStore,
)
from agent_project.web.settings import WebSettings
from agent_project.web.tool_executor import WebToolExecutor

STATIC_ROOT = Path(__file__).parent / "static"


class UIStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        if any(part.startswith(".") for part in Path(path).parts):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


def create_app(*, settings: WebSettings | None = None) -> FastAPI:
    """Create a guarded app without file/network/model side effects.

    Startup acquires the separate lifetime lock. No RunStore, workers, domain
    HTTP operations or UI screen is implemented by the session bootstrap.
    """
    if settings is not None and not isinstance(settings, WebSettings):
        raise TypeError("settings must be an application-owned WebSettings object")
    config = settings if settings is not None else WebSettings()

    def reset_runtime(app):
        if hasattr(app.state, "runner") and app.state.runner is not None:
            app.state.runner.shutdown(wait=False)
        app.state.instance_id = secrets.token_urlsafe(32)
        app.state.sessions = SessionStore()
        app.state.runs = RunStore()
        app.state.reads = TransactionReadService(
            data_path=config.data_path, changes_path=config.changes_path
        )
        app.state.approvals = ApprovalStore()

        def web_tool_executor_factory(
            *, run_id, owner_session_id, on_awaiting_approval, on_resumed
        ):
            """Bind every run to the current shared one-time decision store."""

            def on_registered(pending: PendingAction) -> None:
                # Lifecycle trace keep only catalog-safe identifiers: never
                # the owner session, transport context or validated arguments.
                trace_run_event(
                    app.state.runs,
                    run_id,
                    "approval_raised",
                    {
                        "approval_id": pending.approval_id,
                        "kind": pending.kind,
                        "tool": pending.tool,
                        "action_id": pending.action_id,
                        "transaction_id": pending.transaction.transaction_id,
                    },
                )

            def on_resolution(receipt: DecisionReceipt) -> None:
                trace_run_event(
                    app.state.runs,
                    run_id,
                    "approval_resolved",
                    {"approval_id": receipt.approval_id, "status": receipt.status},
                )

            return WebToolExecutor(
                approvals=app.state.approvals,
                data_path=config.data_path,
                changes_path=config.changes_path,
                run_id=run_id,
                owner_session_id=owner_session_id,
                on_awaiting_approval=on_awaiting_approval,
                on_resumed=on_resumed,
                on_registered=on_registered,
                on_resolution=on_resolution,
            )

        app.state.runner = RunManager(
            runs=app.state.runs,
            data_path=config.data_path,
            changes_path=config.changes_path,
            tool_executor_factory=web_tool_executor_factory,
        )

    @asynccontextmanager
    async def lifespan(app):
        with WebInstanceLock(config.changes_path):
            reset_runtime(app)
            try:
                yield
            finally:
                if hasattr(app.state, "runner") and app.state.runner is not None:
                    app.state.runner.shutdown(wait=False)

    app = FastAPI(
        title="Household Expense Checker",
        version="0.1.0",
        debug=False,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = config
    reset_runtime(app)
    authority = config.host if config.port == 80 else f"{config.host}:{config.port}"
    app.add_middleware(LocalSecurityMiddleware, authority=authority)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        status, body = http_error("invalid_request")
        return JSONResponse(body.model_dump(), status_code=status)

    @app.exception_handler(HTTPException)
    async def http_failure(request, exc):
        if exc.status_code == 404:
            code = "resource_unavailable"
        elif exc.status_code == 405:
            code = "method_not_allowed"
        elif exc.status_code == 403:
            code = "forbidden"
        else:
            code = "internal_error"
        status, body = http_error(code)
        return JSONResponse(body.model_dump(), status_code=status)

    @app.get("/api/session", response_model=SessionResponse)
    async def session_bootstrap(request: Request, response: Response):
        try:
            session, created = app.state.sessions.bootstrap(
                request.cookies.get(SESSION_COOKIE)
            )
        except SessionCapacityError:
            status, body = http_error("state_busy")
            return JSONResponse(body.model_dump(), status_code=status)
        if created:
            response.set_cookie(
                SESSION_COOKIE,
                session.session_id,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
        active_run_id = app.state.runs.get_active_run_id(
            owner_session_id=session.session_id
        )
        return SessionResponse(
            csrf_token=session.csrf_token,
            instance_id=app.state.instance_id,
            active_run_id=active_run_id,
        )

    @app.post(
        "/api/runs",
        status_code=202,
        response_model=RunReceipt,
        response_model_exclude_none=True,
    )
    async def start_run(body: StartRunRequest, request: Request):
        """Accept exactly one analysis start; replays return the same run.

        The security boundary has already verified the session, the same-origin
        context, CSRF and the current instance identifier before this handler
        runs. No GET ever starts a run, and the 202 here means accepted, never
        that the analysis or any write has succeeded.
        """
        browser_session = request.state.browser_session
        try:
            run, _created = app.state.runner.submit_run(
                owner_session_id=browser_session.session_id,
                request_id=body.request_id,
                goal=body.goal,
            )
        except RunInProgressError:
            status, error = http_error("run_in_progress")
            return JSONResponse(error.model_dump(), status_code=status)
        except RequestIdConflictError:
            status, error = http_error("request_id_conflict")
            return JSONResponse(error.model_dump(), status_code=status)
        return run.to_receipt()

    @app.get(
        "/api/runs/{run_id}",
        response_model=RunSnapshot,
        response_model_exclude_none=True,
    )
    async def read_run(run_id: str, request: Request):
        """One owned snapshot of the real run state, version and result.

        Unknown runs and runs owned by another session are indistinguishable
        safe 404 responses, and no read request ever starts or restarts work.
        """
        browser_session = request.state.browser_session
        try:
            snapshot = app.state.runs.get_snapshot(
                run_id, owner_session_id=browser_session.session_id
            )
        except RunAccessDeniedError:
            snapshot = None
        if snapshot is None:
            status, error = http_error("run_unavailable")
            return JSONResponse(error.model_dump(), status_code=status)
        return snapshot

    @app.get(
        "/api/runs/{run_id}/approvals",
        response_model=ApprovalList,
        response_model_exclude_none=True,
    )
    async def list_approvals(run_id: str, request: Request):
        """Pending decision cards for one owned run; presentation only.

        The run itself must exist and belong to the browser session, so a
        foreign run is an identical safe 404. Cards carry operation kind, the
        server-side action proposal, the transaction summary and the TTL end;
        no owner, fingerprint or executable arguments ever leave the backend.
        """
        browser_session = request.state.browser_session
        try:
            snapshot = app.state.runs.get_snapshot(
                run_id, owner_session_id=browser_session.session_id
            )
        except RunAccessDeniedError:
            snapshot = None
        if snapshot is None:
            status, error = http_error("run_unavailable")
            return JSONResponse(error.model_dump(), status_code=status)
        try:
            cards = app.state.approvals.list_cards(
                run_id, owner_session_id=browser_session.session_id
            )
        except ApprovalAccessDeniedError:
            status, error = http_error("approval_unavailable")
            return JSONResponse(error.model_dump(), status_code=status)
        return ApprovalList(approvals=cards)

    @app.post(
        "/api/runs/{run_id}/approvals/{approval_id}",
        response_model=DecisionReceipt,
        response_model_exclude_none=True,
    )
    async def decide_approval(
        run_id: str, approval_id: str, body: DecisionRequest, request: Request
    ):
        """Record one explicit approve/reject for one one-time decision.

        The request body carries only the decision and the instance
        identifier: no goal, tool name, confirmation flag, override flag or
        operation arguments are accepted from the browser. Unknown or foreign
        decisions are an identical safe 404, and a repeated or expired
        decision reads the stored status and result instead of executing
        anything again. The actual execution stays backend-controlled.
        """
        browser_session = request.state.browser_session
        try:
            receipt = app.state.approvals.decide(
                run_id,
                approval_id,
                owner_session_id=browser_session.session_id,
                decision=body.decision,
            )
        except (ApprovalNotFoundError, ApprovalAccessDeniedError):
            status, error = http_error("approval_unavailable")
            return JSONResponse(error.model_dump(), status_code=status)
        return receipt

    @app.get(
        "/api/transactions",
        response_model=TransactionList,
        response_model_exclude_none=True,
    )
    async def list_transactions(request: Request):
        """Model-free effective-state read with an explicit query adapter.

        HTTP query strings are text; only the integer ``limit`` is explicitly
        converted, and every filter still reaches the shared controller so
        types and bounds are never weakened. No read writes or starts work.
        """
        try:
            parsed = parse_read_query(list(request.query_params.multi_items()))
            limit = parse_limit(parsed.get("limit"))
        except ValueError:
            status, error = http_error("invalid_request")
            return JSONResponse(error.model_dump(), status_code=status)
        try:
            items, possibly_truncated = app.state.reads.search(
                query=parsed.get("query"),
                merchant=parsed.get("merchant"),
                limit=limit,
            )
        except ReadUnavailableError as unavailable:
            return JSONResponse(
                unavailable.error.model_dump(), status_code=unavailable.status
            )
        return TransactionList(
            items=items, limit=limit, possibly_truncated=possibly_truncated
        )

    @app.get(
        "/api/transactions/{transaction_id}",
        response_model=TransactionDetails,
        response_model_exclude_none=True,
    )
    async def read_transaction(transaction_id: str, request: Request):
        """One effective-state details card; unknown ids are plain 404s."""
        del request
        try:
            card = app.state.reads.details(transaction_id)
        except ReadUnavailableError as unavailable:
            return JSONResponse(
                unavailable.error.model_dump(), status_code=unavailable.status
            )
        if card is None:
            status, error = http_error("transaction_unavailable")
            return JSONResponse(error.model_dump(), status_code=status)
        return card

    @app.get("/", include_in_schema=False)
    async def ui_screen() -> FileResponse:
        """Static single screen; opening it never runs the model or a domain tool."""
        return FileResponse(
            STATIC_ROOT / "index.html",
            media_type="text/html",
            headers={"cache-control": "no-store"},
        )

    app.mount(
        "/static",
        UIStaticFiles(directory=STATIC_ROOT, check_dir=False, follow_symlink=False),
        name="static",
    )
    return app
