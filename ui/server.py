"""Compatibility entry point for source-checkout dashboard users."""

from verdict.dashboard.app import build_bundle, create_app, main

__all__ = ["app", "build_bundle", "create_app", "main"]

app = create_app()


if __name__ == "__main__":
    raise SystemExit(main())
