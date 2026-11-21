"""Start the single-process, same-origin loopback web service."""

import argparse
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from agent_project.web.app import create_app
from agent_project.web.settings import WebSettings


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Household Expense Checker local web service. "
            "Single-process loopback access with "
            "Host, Origin, session and CSRF controls."
        ),
    )
    parser.add_argument(
        "--host",
        choices=["127.0.0.1"],
        default="127.0.0.1",
        help="Canonical loopback host.",
    )
    parser.add_argument("--port", type=int, default=8765, help="Local loopback port.")
    parser.add_argument("--data-path", type=Path, default=None)
    parser.add_argument("--changes-path", type=Path, default=None)
    args = parser.parse_args(argv)
    paths = {
        name: value
        for name, value in (
            ("data_path", args.data_path),
            ("changes_path", args.changes_path),
        )
        if value is not None
    }
    try:
        settings = WebSettings(host=args.host, port=args.port, **paths)
    except ValidationError:
        parser.error("Invalid local web configuration.")
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=False,
        proxy_headers=False,
        access_log=False,
        server_header=False,
        lifespan="on",
    )


if __name__ == "__main__":
    main()
