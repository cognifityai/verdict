"""Compatibility wrapper for the installed ``verdict-pipeline`` command."""

from verdict_eval.cli.pipeline import build_parser, main

__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
