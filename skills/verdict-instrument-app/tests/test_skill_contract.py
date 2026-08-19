from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[1]


def test_skill_frontmatter_and_agent_metadata_match() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert skill.startswith("---\n")
    frontmatter = skill.split("---\n", 2)[1]
    assert re.search(r"^name: verdict-instrument-app$", frontmatter, re.MULTILINE)
    assert re.search(r"^description: .+", frontmatter, re.MULTILINE)
    assert "$verdict-instrument-app" in metadata


def test_every_relative_markdown_link_resolves_inside_skill() -> None:
    missing: list[str] = []
    for markdown in SKILL_ROOT.rglob("*.md"):
        body = markdown.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", body):
            if "://" in target or target.startswith("#"):
                continue
            resolved = (markdown.parent / target.split("#", 1)[0]).resolve()
            if not resolved.exists():
                missing.append(f"{markdown.relative_to(SKILL_ROOT)} -> {target}")

    assert missing == []


def test_skill_uses_resolved_paths_for_repo_local_commands() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "python3 <skill-root>/scripts/scan_repository.py" in skill
    assert "<verdict-repo>/scripts/run_drift_pipeline.py --help" in skill
    assert "python3 scripts/scan_repository.py" not in skill
    assert re.search(r"Resolve\s+`<verdict-repo>` independently", skill)
    assert "native skill installer may" in skill


def test_documented_pipeline_flags_match_the_released_parser() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "run_drift_pipeline.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--storage" in result.stdout
    assert "--trust-existing-clusters" in result.stdout
    assert "--yes-spend" not in result.stdout
    assert "--max-spend-usd" not in result.stdout


def test_customer_entry_document_points_to_the_committed_skill() -> None:
    guide = (REPO_ROOT / "docs" / "AGENT_POC_SKILL.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "skills/verdict-instrument-app/SKILL.md" in guide
    assert "docs/AGENT_POC_SKILL.md" in readme
    assert "does not automatically install a skill in every coding agent" in guide
    assert re.search(
        r"does not install the\s+repository-local pipeline or dashboard",
        guide,
    )
