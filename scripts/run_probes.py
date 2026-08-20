"""Compatibility wrapper for the installed ``verdict-probes`` command."""

from verdict_eval.cli.probes import main, probe_run_exit_code, probe_run_payload

__all__ = ["main", "probe_run_exit_code", "probe_run_payload"]


if __name__ == "__main__":
    raise SystemExit(main())
