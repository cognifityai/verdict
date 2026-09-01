"""The opinionated local product entry point: launch UI and open a browser."""

from __future__ import annotations

import sys

from verdict.dashboard import app


def main(argv: list[str] | None = None) -> int:
    return app.main(["--open-browser", *(sys.argv[1:] if argv is None else argv)])
