#!/usr/bin/env python3
"""Read-only candidate scanner for Verdict instrumentation planning.

The report contains file paths, line numbers, and rule metadata only. It never emits
source snippets or matched values because repository text may contain secrets.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import sys
import tokenize
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 1_000_000
DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_FINDINGS = 5_000

IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}

SOURCE_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".rs": "rust",
    ".php": "php",
    ".cs": "csharp",
}

TEXT_FILE_NAMES = {
    "Dockerfile",
    "Procfile",
    "Pipfile",
    "poetry.lock",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "uv.lock",
}

TEXT_EXTENSIONS = {
    *SOURCE_LANGUAGES,
    ".cfg",
    ".conf",
    ".ini",
    ".json",
    ".lock",
    ".properties",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    pattern: re.Pattern[str]
    finding: str
    support: str
    remediation: str
    source_only: bool = True
    python_supported: bool = False


@dataclass(frozen=True)
class Finding:
    rule_id: str
    category: str
    path: str
    line: int
    language: str | None
    support: str
    finding: str
    remediation: str


def _rule(
    rule_id: str,
    category: str,
    pattern: str,
    finding: str,
    support: str,
    remediation: str,
    *,
    source_only: bool = True,
    python_supported: bool = False,
) -> Rule:
    return Rule(
        rule_id=rule_id,
        category=category,
        pattern=re.compile(pattern, re.IGNORECASE),
        finding=finding,
        support=support,
        remediation=remediation,
        source_only=source_only,
        python_supported=python_supported,
    )


RULES: tuple[Rule, ...] = (
    _rule(
        "anthropic-messages-create",
        "provider-call",
        r"\.messages\.create\s*\(",
        "Candidate Anthropic messages.create call.",
        "supported",
        "Confirm the object is an Anthropic sync/async client and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "anthropic-messages-stream",
        "provider-call",
        r"\.messages\.stream\s*\(",
        "Candidate Anthropic messages.stream helper call.",
        "supported-with-constraints",
        "Confirm the object is an Anthropic sync/async client, consume or close the helper, and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "openai-chat-completions-create",
        "provider-call",
        r"\.chat\.completions\.create\s*\(",
        "Candidate OpenAI chat.completions.create call.",
        "supported",
        "Confirm the object is an OpenAI sync/async client and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "openai-chat-completions-stream",
        "provider-call",
        r"\.chat\.completions\.stream\s*\(",
        "Candidate OpenAI chat.completions.stream helper call.",
        "supported-with-constraints",
        "Confirm the object is an OpenAI sync/async client, consume or close the stream, and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "openai-responses-create",
        "provider-call",
        r"\.responses\.create\s*\(",
        "Candidate OpenAI Responses API call.",
        "supported",
        "Confirm the object is an OpenAI sync/async client and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "openai-responses-parse",
        "provider-call",
        r"\.responses\.parse\s*\(",
        "Candidate OpenAI Responses parse call.",
        "supported",
        "Confirm the object is an OpenAI sync/async client and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "openai-responses-stream",
        "provider-call",
        r"\.responses\.stream\s*\(",
        "Candidate OpenAI Responses API stream call.",
        "supported-with-constraints",
        "Confirm the object is an OpenAI sync/async client, consume or close the stream, and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "openai-responses-raw-manager",
        "provider-call",
        r"\.responses\.with_streaming_response\.(?:create|parse)\s*\(",
        "OpenAI Responses raw-response manager call is outside the a8 surface.",
        "conflict",
        "Use a released Responses create, parse, or stream path and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "google-generate-content",
        "provider-call",
        r"\.models\.generate_content\s*\(",
        "Candidate google-genai generate_content call.",
        "supported",
        "Confirm the object is a google-genai client and exercise final storage.",
        python_supported=True,
    ),
    _rule(
        "google-generate-content-stream",
        "provider-call",
        r"\.models\.generate_content_stream\s*\(",
        "Candidate google-genai generate_content_stream call.",
        "supported",
        "Confirm the object is a google-genai client and exercise stream completion and final storage.",
        python_supported=True,
    ),
    _rule(
        "google-legacy-generate-content",
        "provider-call",
        r"(?<!\.models)\.generate_content\s*\(",
        "Candidate legacy Google GenerativeModel generate_content call.",
        "supported-with-constraints",
        "Confirm the receiver is a legacy GenerativeModel; generic methods with this name are not proof.",
        python_supported=True,
    ),
    _rule(
        "verdict-init",
        "verdict-lifecycle",
        r"\bverdict\.init\s*\(",
        "Verdict process initialization candidate.",
        "review",
        "Prove it runs once per process before every supported provider call.",
        python_supported=True,
    ),
    _rule(
        "verdict-shutdown-not-exported",
        "verdict-lifecycle",
        r"\bverdict\.shutdown\s*\(",
        "verdict.shutdown is not exported in the target release.",
        "conflict",
        "Import shutdown from verdict.client, then exercise normal exit and cancellation.",
        python_supported=True,
    ),
    _rule(
        "verdict-client-shutdown-import",
        "verdict-lifecycle",
        r"\bfrom\s+verdict\.client\s+import\s+[^\n]*\bshutdown\b",
        "Released Verdict shutdown import candidate.",
        "review",
        "Exercise normal exit and cancellation when buffered writes are enabled.",
        python_supported=True,
    ),
    _rule(
        "content-capture-enabled",
        "privacy-config",
        r"\bcapture_content\s*=\s*True\b",
        "Prompt/response content capture appears enabled.",
        "risk",
        "Require separate approval, retention, recursive canary tests, and documented redaction gaps.",
        python_supported=True,
    ),
    _rule(
        "buffered-writes-enabled",
        "durability-config",
        r"\bbuffered_writes\s*=\s*True\b",
        "Buffered Verdict writes appear enabled.",
        "risk",
        "Require explicit shutdown wiring and final-store failure tests.",
        python_supported=True,
    ),
    _rule(
        "relative-sqlite-uri",
        "storage-config",
        r"sqlite:///(?!/|[a-z]:[/\\])[^\s\"']+",
        "A relative SQLite URI may depend on process working directory.",
        "risk",
        "Resolve and configure a customer-owned absolute SQLite path.",
        source_only=False,
    ),
    _rule(
        "postgres-storage-uri",
        "storage-config",
        r"postgres(?:ql)?(?:\+[a-z0-9_]+)?://",
        "A Postgres storage URI candidate is present.",
        "supported-with-constraints",
        "Keep secrets out of output; the bundled dashboard reads PostgreSQL directly when the postgres extra is installed.",
        source_only=False,
    ),
    _rule(
        "wrong-verdict-distribution",
        "dependency",
        r"(?m)(?:^\s*verdict(?:\[[^\]\n]+\])?(?:\s*[=<>!~^].*)?\s*$|[\"']verdict(?:\[[^\]\n]+\])?(?:\s*[=<>!~^][^\"'\n]*)?[\"'])",
        "Dependency may install the unrelated PyPI distribution named verdict.",
        "conflict",
        "Use cognifity-verdict and verify the imported module distribution.",
        source_only=False,
    ),
    _rule(
        "cognifity-verdict-distribution",
        "dependency",
        r"\bcognifity-verdict(?:-eval|-inspect)?\b",
        "Cognifity Verdict distribution candidate is present.",
        "review",
        "Confirm compatible versions and the installed import owner.",
        source_only=False,
    ),
    _rule(
        "web-process",
        "lifecycle-candidate",
        r"\b(?:FastAPI|Flask|Django|uvicorn|gunicorn)\b",
        "Web-process lifecycle candidate.",
        "review",
        "Locate the process startup owner and initialize before provider calls.",
    ),
    _rule(
        "worker-process",
        "lifecycle-candidate",
        r"\b(?:Celery|rq\.Queue|dramatiq|arq)\b",
        "Background-worker lifecycle candidate.",
        "review",
        "Instrument the worker bootstrap separately from the web process.",
    ),
    _rule(
        "serverless-process",
        "lifecycle-candidate",
        r"\b(?:AWS_LAMBDA|lambda_handler|functions_framework|cloudfunctions)\b",
        "Serverless lifecycle candidate.",
        "review",
        "Verify cold/warm lifecycle, filesystem durability, and flush behavior.",
    ),
    _rule(
        "scheduler-config",
        "scheduler-candidate",
        r"(?:\bschedule\s*:|\bcron\s*:|\bcrontab\b|\bCronJob\b|\bworkflow_dispatch\b|\bsystemd\.timer\b)",
        "Existing scheduler convention candidate.",
        "review",
        "Reuse the existing scheduler and define lock, timeout, logs, identity, and rollback.",
        source_only=False,
    ),
)

DRIFT_COMMAND_RULES: tuple[Rule, ...] = (
    _rule(
        "drift-pipeline-db-flag",
        "scheduler-config",
        r"--db(?:\s|=)",
        "The released drift pipeline does not accept the dashboard's --db flag.",
        "conflict",
        "Use --storage or VERDICT_STORAGE with verdict-pipeline; --db is only a legacy dashboard wrapper flag.",
        source_only=False,
    ),
    _rule(
        "unsupported-drift-budget-flag",
        "scheduler-config",
        r"(?:--yes-spend|--max-spend-usd)(?:\s|=|$)",
        "The released drift pipeline does not accept this spend-control flag.",
        "conflict",
        "Remove unsupported flags; enforce approval, call ceilings, timeout, and budget outside the runner.",
        source_only=False,
    ),
)


def language_for(path: Path) -> str | None:
    return SOURCE_LANGUAGES.get(path.suffix.lower())


def is_text_candidate(path: Path) -> bool:
    return path.name in TEXT_FILE_NAMES or path.suffix.lower() in TEXT_EXTENSIONS


def is_python_dependency_manifest(path: Path) -> bool:
    name = path.name.lower()
    return (
        name
        in {
            "pipfile",
            "poetry.lock",
            "pyproject.toml",
            "setup.cfg",
            "setup.py",
            "uv.lock",
        }
        or (name.startswith("requirements") and name.endswith(".txt"))
        or (name.startswith("constraints") and name.endswith(".txt"))
    )


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    offsets.extend(index + 1 for index, char in enumerate(text) if char == "\n")
    return offsets


def _mask_span(
    characters: list[str],
    offsets: list[int],
    start: tuple[int, int],
    end: tuple[int, int],
) -> None:
    start_index = offsets[start[0] - 1] + start[1]
    end_index = offsets[end[0] - 1] + end[1]
    for index in range(start_index, min(end_index, len(characters))):
        if characters[index] not in "\r\n":
            characters[index] = " "


def python_search_views(text: str) -> tuple[str, str, bool]:
    """Return executable and configuration views without exposing source text.

    The executable view masks comments and every string literal so provider/lifecycle
    rules do not fire on examples. The configuration view preserves ordinary strings
    such as storage URLs and command arrays, but masks comments and docstrings.
    """

    offsets = _line_offsets(text)
    executable = list(text)
    configuration = list(text)
    tokens: list[tokenize.TokenInfo] = []
    parse_fallback = False

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (IndentationError, tokenize.TokenError):
        parse_fallback = True

    for token in tokens:
        if token.type == tokenize.COMMENT:
            _mask_span(executable, offsets, token.start, token.end)
            _mask_span(configuration, offsets, token.start, token.end)
        elif token.type == tokenize.STRING:
            _mask_span(executable, offsets, token.start, token.end)

    try:
        tree = ast.parse(text)
    except (IndentationError, SyntaxError, ValueError):
        parse_fallback = True
    else:
        owners = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        for node in ast.walk(tree):
            if not isinstance(node, owners) or not node.body:
                continue
            first = node.body[0]
            if not (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                continue
            end_line = getattr(first, "end_lineno", first.lineno)
            end_col = getattr(first, "end_col_offset", first.col_offset)
            _mask_span(
                configuration,
                offsets,
                (first.lineno, first.col_offset),
                (end_line, end_col),
            )

    return "".join(executable), "".join(configuration), parse_fallback


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def iter_drift_command_blocks(text: str) -> Iterable[tuple[int, str]]:
    """Yield bounded shell/YAML command blocks containing the drift runner."""

    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if "run_drift_pipeline.py" not in line and "verdict-pipeline" not in line:
            continue

        base_indent = _indent_width(line)
        previous_nonempty = ""
        for prior in reversed(lines[max(0, index - 20) : index]):
            if prior.strip():
                previous_nonempty = prior
                break
        yaml_folded = bool(re.search(r":\s*[>|][-+]?\s*$", previous_nonempty))
        prefix = "".join(lines[max(0, index - 20) : index])
        shell_array = prefix.count("(") > prefix.count(")")

        block = [line]
        previous = line
        for candidate in lines[index + 1 : index + 31]:
            stripped = candidate.strip()
            if not stripped:
                break
            indent = _indent_width(candidate)
            yaml_item = (
                line.lstrip().startswith("-") and candidate.lstrip().startswith("-")
            )
            continuation = (
                previous.rstrip().endswith("\\")
                or indent > base_indent
                or stripped.startswith("--")
                or yaml_item
                or yaml_folded
                or shell_array
            )
            if yaml_folded and indent < base_indent:
                continuation = False
            if not continuation:
                break
            block.append(candidate)
            previous = candidate
            if shell_array and stripped == ")":
                break

        yield index + 1, " ".join(part.strip() for part in block)


def iter_files(root: Path, max_files: int) -> Iterable[Path]:
    seen = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in IGNORED_DIRECTORIES
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(files):
            path = Path(current) / name
            if path.is_symlink() or not path.is_file() or not is_text_candidate(path):
                continue
            seen += 1
            if seen > max_files:
                raise RuntimeError(
                    f"scan exceeded --max-files={max_files}; narrow the root or raise the explicit limit"
                )
            yield path


def scan(
    root: Path,
    max_files: int,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> dict[str, object]:
    findings: list[Finding] = []
    skipped_large = 0
    skipped_unreadable = 0
    python_parse_fallback_files = 0
    scanned_files = 0

    for path in iter_files(root, max_files):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped_large += 1
                continue
            raw = path.read_bytes()
            if b"\x00" in raw:
                skipped_unreadable += 1
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            skipped_unreadable += 1
            continue

        scanned_files += 1
        language = language_for(path)
        relative_path = path.relative_to(root).as_posix()
        line_starts = _line_offsets(text)
        executable_text = text
        configuration_text = text
        if language == "python":
            executable_text, configuration_text, parse_fallback = python_search_views(
                text
            )
            if parse_fallback:
                python_parse_fallback_files += 1
                if len(findings) >= max_findings:
                    raise RuntimeError(
                        "scan exceeded "
                        f"--max-findings={max_findings}; narrow the root or raise the explicit limit"
                    )
                findings.append(
                    Finding(
                        rule_id="python-parse-fallback",
                        category="scanner-limit",
                        path=relative_path,
                        line=1,
                        language=language,
                        support="review",
                        finding="Python parsing was incomplete for this file.",
                        remediation=(
                            "Review this file manually; executable-code masking may be incomplete."
                        ),
                    )
                )

        for rule in RULES:
            if rule.source_only and language is None:
                continue
            if rule.category == "dependency" and not is_python_dependency_manifest(
                path
            ):
                continue
            search_text = executable_text if rule.source_only else configuration_text
            for match in rule.pattern.finditer(search_text):
                if len(findings) >= max_findings:
                    raise RuntimeError(
                        "scan exceeded "
                        f"--max-findings={max_findings}; narrow the root or raise the explicit limit"
                    )
                support = rule.support
                remediation = rule.remediation
                if rule.python_supported and language != "python":
                    support = "unsupported"
                    remediation = (
                        "Released auto-instrumentation is Python-only; classify this path as "
                        "unsupported or propose a separately approved unverified adapter."
                    )
                findings.append(
                    Finding(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        path=relative_path,
                        line=bisect_right(line_starts, match.start()),
                        language=language,
                        support=support,
                        finding=rule.finding,
                        remediation=remediation,
                    )
                )

        for start_line, command in iter_drift_command_blocks(configuration_text):
            for rule in DRIFT_COMMAND_RULES:
                if not rule.pattern.search(command):
                    continue
                if len(findings) >= max_findings:
                    raise RuntimeError(
                        "scan exceeded "
                        f"--max-findings={max_findings}; narrow the root or raise the explicit limit"
                    )
                findings.append(
                    Finding(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        path=relative_path,
                        line=start_line,
                        language=language,
                        support=rule.support,
                        finding=rule.finding,
                        remediation=rule.remediation,
                    )
                )

    findings.sort(key=lambda item: (item.path, item.line, item.rule_id))
    counts: dict[str, int] = {}
    for item in findings:
        counts[item.support] = counts.get(item.support, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "scanned_files": scanned_files,
        "skipped_large_files": skipped_large,
        "skipped_unreadable_files": skipped_unreadable,
        "python_parse_fallback_files": python_parse_fallback_files,
        "finding_counts_by_support": dict(sorted(counts.items())),
        "findings": [asdict(item) for item in findings],
        "limitations": [
            "Pattern matches are candidates and require call-graph validation.",
            "Dynamic imports, aliases, wrappers, generated clients, and non-text files may be missed.",
            "Malformed Python may use a conservative token-only fallback; inspect those files manually.",
            "No source snippets or matched values are emitted.",
            "An empty report is not proof that the application makes no LLM calls.",
        ],
    }


def render_text(report: dict[str, object]) -> str:
    lines = [
        f"Verdict instrumentation scan schema {report['schema_version']}",
        f"Root: {report['root']}",
        f"Scanned files: {report['scanned_files']}",
        f"Python parse fallbacks: {report['python_parse_fallback_files']}",
        f"Findings: {len(report['findings'])}",
    ]
    for item in report["findings"]:  # type: ignore[union-attr]
        lines.append(
            f"{item['support']:>26}  {item['path']}:{item['line']}  "
            f"{item['rule_id']} — {item['finding']}"
        )
    lines.append("Review every candidate in context; no source snippets were emitted.")
    return "\n".join(lines)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find candidate Verdict instrumentation and operational paths without emitting source text."
    )
    parser.add_argument("root", type=Path, help="Repository directory to scan")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-findings", type=int, default=DEFAULT_MAX_FINDINGS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"error: repository root is not a directory: {root}", file=sys.stderr)
        return 2
    if args.max_files < 1:
        print("error: --max-files must be positive", file=sys.stderr)
        return 2
    if args.max_findings < 1:
        print("error: --max-findings must be positive", file=sys.stderr)
        return 2
    try:
        report = scan(root, args.max_files, args.max_findings)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
