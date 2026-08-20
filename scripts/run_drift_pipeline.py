"""Source wrapper for an environment with ``cognifity-verdict-eval`` installed."""

from verdict_eval.cli.pipeline import build_parser, main

__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
