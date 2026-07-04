"""verdict-inspect CLI.

Usage:
    verdict-inspect analyze <path> [--format FORMAT] [--report PATH] [--json] [--no-judge] [--no-semantic]

Examples:
    verdict-inspect analyze ~/Downloads/conversations.json
    verdict-inspect analyze --format claude_ai ~/Downloads/claude_export.json
    verdict-inspect analyze --json ~/cursor_chats.jsonl | jq .
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from verdict_inspect.parsers.autodetect import detect_format, parse
from verdict_inspect.pipeline import run_inspect
from verdict_inspect.report import to_json, to_markdown


@click.group()
@click.version_option()
def main() -> None:
    """Verdict Inspect — drift / quality analysis on chat exports."""


@main.command()
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--format", "fmt",
    type=click.Choice(["auto", "chatgpt", "claude_ai", "cowork", "openai_jsonl"]),
    default="auto",
    help="Chat export format. 'auto' (default) sniffs the file.",
)
@click.option(
    "--report", "report_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write Markdown report to this path. If omitted, prints to stdout.",
)
@click.option(
    "--json", "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of Markdown (for piping to jq, etc.).",
)
@click.option(
    "--no-judge",
    is_flag=True,
    default=False,
    help="Skip the LLM-as-judge layer (still emits structural + semantic).",
)
@click.option(
    "--no-semantic",
    is_flag=True,
    default=False,
    help="Skip the semantic-drift layer (still emits structural + judge).",
)
@click.option(
    "--judge-model",
    default="claude-haiku-4-5-20251001",
    help="Anthropic model to use as judge.",
)
def analyze(
    path: Path,
    fmt: str,
    report_path: Path | None,
    as_json: bool,
    no_judge: bool,
    no_semantic: bool,
    judge_model: str,
) -> None:
    """Analyze a chat export for drift / quality signals."""
    if fmt == "auto":
        try:
            fmt = detect_format(path)
        except ValueError as e:
            raise click.UsageError(str(e))
        click.echo(f"Detected format: {fmt}", err=True)
    conversations = parse(path, fmt)
    if not conversations:
        raise click.UsageError(f"No conversations parsed from {path}")

    click.echo(
        f"Parsed {len(conversations)} conversation(s) "
        f"({sum(c.n_turns for c in conversations)} assistant turns total).",
        err=True,
    )

    report = run_inspect(
        conversations,
        format_name=fmt,
        judge_model=judge_model,
        enable_semantic=not no_semantic,
        enable_judge=not no_judge,
    )

    output = to_json(report) if as_json else to_markdown(report)

    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(output)
        click.echo(f"Report written: {report_path}", err=True)
    else:
        click.echo(output)


@main.command()
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def detect(path: Path) -> None:
    """Print the detected format for a file and exit."""
    fmt = detect_format(path)
    click.echo(fmt)


if __name__ == "__main__":
    main()
