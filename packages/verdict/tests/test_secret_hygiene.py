"""Repository controls for provider credentials and build contexts."""

import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.check_release_artifacts import main, private_members, scan_artifact

ROOT = Path(__file__).resolve().parents[3]
PROVIDER_KEY_NAMES = {
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
}
FINAL_SECRET_DENIES = [
    "*.[Ee][Nn][Vv]*",
    ".[Dd][Ii][Rr][Ee][Nn][Vv]/",
]


def _active_patterns(name: str) -> list[str]:
    return [
        line.strip()
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_provider_key_example_is_complete_and_contains_only_placeholders():
    assignments = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        assert separator, f"unexpected non-assignment in .env.example: {line!r}"
        assert value == "", f".env.example value must be empty for {name}"
        assert name not in assignments, f"duplicate .env.example assignment: {name}"
        assignments[name] = value

    assert set(assignments) == PROVIDER_KEY_NAMES


def test_no_plaintext_environment_file_exists_in_the_worktree():
    forbidden = []
    for directory, child_directories, filenames in os.walk(ROOT):
        child_directories[:] = [
            name for name in child_directories if name not in {".git", ".venv", "node_modules"}
        ]
        current = Path(directory)
        for name in [*child_directories, *filenames]:
            path = current / name
            if path == ROOT / ".env.example":
                continue
            lowered = name.lower()
            if (
                ".env" in lowered
                or lowered == ".direnv"
            ):
                forbidden.append(path.relative_to(ROOT).as_posix())

    assert sorted(forbidden) == []


def test_git_ignores_plaintext_environment_and_direnv_state():
    patterns = _active_patterns(".gitignore")

    assert "*.[Ee][Nn][Vv]*" in patterns
    assert "!/.env.example" in patterns
    assert ".[Dd][Ii][Rr][Ee][Nn][Vv]/" in patterns

    for candidate in (
        ".env",
        "nested/.EnV.LoCaL",
        "nested/service.env",
        "nested/SERVICE.EnV.LoCaL",
        "nested/.env~",
        "nested/service.env~",
        "nested/service.env-backup",
        "nested/service.env_local",
        "nested/service.EnVrC",
        "nested/.env.example",
        ".DiReNv/provider-key",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", candidate],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, candidate

    example = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", ".env.example"],
        cwd=ROOT,
        check=False,
    )
    assert example.returncode == 1

    for counterexample in ("environment.py", "nested/environment.py"):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", counterexample],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 1, counterexample


def test_container_and_gcloud_contexts_end_with_secret_denies():
    for ignore_file in (".dockerignore", ".gcloudignore"):
        assert _active_patterns(ignore_file)[-len(FINAL_SECRET_DENIES) :] == FINAL_SECRET_DENIES


@pytest.mark.parametrize(
    "member",
    [
        ".env",
        "package/.env.local",
        "package/production.env",
        "package/production.env.local",
        "package/SERVICE.ENV.LOCAL",
        "package/.env~",
        "package/service.env~",
        "package/service.env-backup",
        "package/service.env_local",
        ".envrc",
        "package/.envrc.local",
        "package/service.envrc",
        "package/service.envrc.local",
        "package/SERVICE.ENVRC",
        "package/.ENV",
        "package/.env.local/provider-key",
        "package/service.env.local/provider-key",
        "package/service.envrc.local/provider-key",
        "package/.env.example/provider-key",
        "package\\service.env.local\\provider-key",
        ".direnv/config",
        "package/.DIRENV/provider-key",
        "package/.direnv/provider-key",
        "package/_review_notes.md",
        "package/_sync/private-note.md",
    ],
)
def test_release_artifact_scanner_rejects_private_members(member):
    assert private_members([member]) == [member]


def test_release_artifact_scanner_allows_the_variable_name_example():
    assert private_members(
        ["package/.env.example", "package/verdict/redaction.py", "package/environment.py"]
    ) == []


@pytest.mark.parametrize("archive_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "member",
    [
        "package/.env.local/provider-key",
        "package\\service.envrc.local\\provider-key",
        "package/.env.example/provider-key",
        "package/SERVICE.ENV.LOCAL/provider-key",
        "package/service.env-backup/provider-key",
        "package/.env~",
    ],
)
def test_release_artifact_scanner_reads_supported_archives(tmp_path, archive_kind, member):
    if archive_kind == "wheel":
        artifact = tmp_path / "package.whl"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr(member, "not-a-real-secret")
    else:
        artifact = tmp_path / "package.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            archive.addfile(tarfile.TarInfo(member))

    assert scan_artifact(artifact) == [member]


def test_release_artifact_scanner_fails_closed_without_archives(tmp_path):
    assert main([str(tmp_path)]) == 2


def test_alignment_wrapper_never_sources_repository_environment_file(tmp_path):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    sweep = scripts / "run_alignment_sweep.sh"
    sweep.write_text((ROOT / "scripts/run_alignment_sweep.sh").read_text(encoding="utf-8"))
    sweep.chmod(0o755)
    (project / ".env").write_text("FILE_ONLY_SENTINEL=must-not-load\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\nprintf '%s|%s' \"${PROCESS_SENTINEL-}\" \"${FILE_ONLY_SENTINEL-}\" > \"$CAPTURE_PATH\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    capture = tmp_path / "captured-environment"
    environment = os.environ.copy()
    environment.update(
        {
            "CAPTURE_PATH": str(capture),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "PROCESS_SENTINEL": "injected-by-parent",
        }
    )

    result = subprocess.run(
        ["bash", str(sweep)],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert capture.read_text(encoding="utf-8") == "injected-by-parent|"
