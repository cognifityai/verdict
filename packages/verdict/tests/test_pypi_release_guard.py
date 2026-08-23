from __future__ import annotations

import hashlib
import json
import urllib.error
from pathlib import Path

import pytest

from scripts.pypi_release_guard import (
    ReleaseStateError,
    artifact_hashes,
    fetch_remote_hashes,
    main,
    missing_artifacts,
    remote_hashes,
    stage_artifacts,
    write_github_output,
)

WHEEL = "package-1-py3-none-any.whl"
SDIST = "package-1.tar.gz"
LOCAL = {WHEEL: "a" * 64, SDIST: "b" * 64}
PACKAGE_A = {"a-1-py3-none-any.whl": "1" * 64, "a-1.tar.gz": "2" * 64}
PACKAGE_B = {"b-1-py3-none-any.whl": "3" * 64, "b-1.tar.gz": "4" * 64}
PACKAGE_C = {"c-1-py3-none-any.whl": "5" * 64, "c-1.tar.gz": "6" * 64}


def _write_reviewed_artifacts(root: Path) -> dict[str, str]:
    (root / WHEEL).write_bytes(b"wheel")
    (root / SDIST).write_bytes(b"sdist")
    return artifact_hashes(root)


def test_artifact_hashes_requires_exact_wheel_and_sdist(tmp_path: Path) -> None:
    assert _write_reviewed_artifacts(tmp_path) == {
        WHEEL: hashlib.sha256(b"wheel").hexdigest(),
        SDIST: hashlib.sha256(b"sdist").hexdigest(),
    }


def test_artifact_hashes_rejects_missing_sdist(tmp_path: Path) -> None:
    (tmp_path / WHEEL).write_bytes(b"wheel")

    with pytest.raises(ReleaseStateError, match="exactly one wheel and one source"):
        artifact_hashes(tmp_path)


def test_artifact_hashes_rejects_duplicate_artifact_type(tmp_path: Path) -> None:
    (tmp_path / WHEEL).write_bytes(b"wheel")
    (tmp_path / "package-1-second-py3-none-any.whl").write_bytes(b"second")
    (tmp_path / SDIST).write_bytes(b"sdist")

    with pytest.raises(ReleaseStateError, match="exactly one wheel and one source"):
        artifact_hashes(tmp_path)


def test_artifact_hashes_rejects_unexpected_extra_file(tmp_path: Path) -> None:
    _write_reviewed_artifacts(tmp_path)
    (tmp_path / "notes.txt").write_text("not an artifact", encoding="utf-8")

    with pytest.raises(ReleaseStateError, match="exactly one wheel and one source"):
        artifact_hashes(tmp_path)


def test_remote_hashes_requires_complete_unique_file_entries() -> None:
    assert remote_hashes(
        {
            "urls": [
                {"filename": WHEEL, "digests": {"sha256": "a" * 64}},
                {"filename": SDIST, "digests": {"sha256": "b" * 64}},
            ]
        }
    ) == LOCAL

    with pytest.raises(ReleaseStateError, match="repeats"):
        remote_hashes(
            {
                "urls": [
                    {"filename": WHEEL, "digests": {"sha256": "a" * 64}},
                    {"filename": WHEEL, "digests": {"sha256": "a" * 64}},
                ]
            }
        )


@pytest.mark.parametrize("payload", ({"urls": []}, {"urls": [{}]}, []))
def test_remote_hashes_rejects_malformed_response(payload: object) -> None:
    with pytest.raises(ReleaseStateError):
        remote_hashes(payload)


@pytest.mark.parametrize(
    ("remote", "expected"),
    (
        (None, (WHEEL, SDIST)),
        ({WHEEL: "a" * 64}, (SDIST,)),
        ({SDIST: "b" * 64}, (WHEEL,)),
        (LOCAL, ()),
    ),
)
def test_missing_artifacts_handles_absent_partial_and_complete_remote_sets(
    remote: dict[str, str] | None, expected: tuple[str, ...]
) -> None:
    assert missing_artifacts(LOCAL, remote) == expected


def test_missing_artifacts_rejects_changed_digest() -> None:
    with pytest.raises(ReleaseStateError, match="changed digest"):
        missing_artifacts(LOCAL, {WHEEL: "c" * 64})


def test_missing_artifacts_rejects_unknown_remote_filename() -> None:
    with pytest.raises(ReleaseStateError, match="unknown release file"):
        missing_artifacts(LOCAL, {"unknown.whl": "a" * 64})


def test_stage_artifacts_copies_only_missing_sdist_after_wheel_upload(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    local = _write_reviewed_artifacts(artifact_dir)
    staging_dir = tmp_path / "publish"

    staged = stage_artifacts(
        artifact_dir,
        staging_dir,
        local,
        missing_artifacts(local, {WHEEL: local[WHEEL]}),
    )

    assert staged == (staging_dir / SDIST,)
    assert (staging_dir / SDIST).read_bytes() == b"sdist"
    assert not (staging_dir / WHEEL).exists()


def test_stage_artifacts_copies_both_files_for_absent_version(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    local = _write_reviewed_artifacts(artifact_dir)
    staging_dir = tmp_path / "publish"

    staged = stage_artifacts(
        artifact_dir,
        staging_dir,
        local,
        missing_artifacts(local, None),
    )

    assert tuple(path.name for path in staged) == (WHEEL, SDIST)
    assert {path.name: path.read_bytes() for path in staged} == {
        WHEEL: b"wheel",
        SDIST: b"sdist",
    }


def test_stage_artifacts_rejects_nonempty_publisher_directory(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    _write_reviewed_artifacts(artifact_dir)
    staging_dir = tmp_path / "publish"
    staging_dir.mkdir()
    (staging_dir / "stale.whl").write_bytes(b"stale")

    with pytest.raises(ReleaseStateError, match="not empty"):
        stage_artifacts(artifact_dir, staging_dir, artifact_hashes(artifact_dir), (WHEEL,))


def test_stage_artifacts_rejects_local_bytes_changed_after_review(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    reviewed = _write_reviewed_artifacts(artifact_dir)
    (artifact_dir / SDIST).write_bytes(b"changed after review")
    staging_dir = tmp_path / "publish"

    with pytest.raises(ReleaseStateError, match="local artifact changed"):
        stage_artifacts(artifact_dir, staging_dir, reviewed, (SDIST,))

    assert list(staging_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("remote_a", "remote_b", "remote_c", "expected"),
    (
        (None, None, None, (True, True, True)),
        (PACKAGE_A, None, None, (False, True, True)),
        (PACKAGE_A, PACKAGE_B, None, (False, False, True)),
        (PACKAGE_A, PACKAGE_B, PACKAGE_C, (False, False, False)),
    ),
)
def test_rerun_after_zero_to_three_completed_package_boundaries(
    remote_a: dict[str, str] | None,
    remote_b: dict[str, str] | None,
    remote_c: dict[str, str] | None,
    expected: tuple[bool, bool, bool],
) -> None:
    assert (
        bool(missing_artifacts(PACKAGE_A, remote_a)),
        bool(missing_artifacts(PACKAGE_B, remote_b)),
        bool(missing_artifacts(PACKAGE_C, remote_c)),
    ) == expected


def test_fetch_remote_hashes_treats_only_404_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def absent(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError("url", 404, "missing", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", absent)
    assert fetch_remote_hashes("package", "1.0") is None

    def unavailable(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError("url", 503, "unavailable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    with pytest.raises(ReleaseStateError, match="HTTP 503"):
        fetch_remote_hashes("package", "1.0")


def test_fetch_remote_hashes_rejects_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> object:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    with pytest.raises(ReleaseStateError, match="PyPI query failed"):
        fetch_remote_hashes("package", "1.0")


def test_fetch_remote_hashes_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, amount: int = -1) -> bytes:
            return b"{"

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ReleaseStateError, match="PyPI query failed"):
        fetch_remote_hashes("package", "1.0")


def test_fetch_remote_hashes_decodes_pypi_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, amount: int = -1) -> bytes:
            return json.dumps(
                {"urls": [{"filename": WHEEL, "digests": {"sha256": "a" * 64}}]}
            ).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    assert fetch_remote_hashes("package", "1.0") == {WHEEL: "a" * 64}


def test_main_stages_only_missing_file_and_sets_publish_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    local = _write_reviewed_artifacts(artifact_dir)
    staging_dir = tmp_path / "publish"
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(
        "scripts.pypi_release_guard.fetch_remote_hashes",
        lambda project, version: {WHEEL: local[WHEEL]},
    )

    result = main(
        [
            "--project",
            "package",
            "--version",
            "1.0",
            "--artifact-dir",
            str(artifact_dir),
            "--staging-dir",
            str(staging_dir),
        ]
    )

    assert result == 0
    assert output.read_text(encoding="utf-8") == "publish=true\n"
    assert [path.name for path in staging_dir.iterdir()] == [SDIST]


def test_main_skips_complete_exact_package_without_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    local = _write_reviewed_artifacts(artifact_dir)
    staging_dir = tmp_path / "publish"
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(
        "scripts.pypi_release_guard.fetch_remote_hashes",
        lambda project, version: local,
    )

    result = main(
        [
            "--project",
            "package",
            "--version",
            "1.0",
            "--artifact-dir",
            str(artifact_dir),
            "--staging-dir",
            str(staging_dir),
        ]
    )

    assert result == 0
    assert output.read_text(encoding="utf-8") == "publish=false\n"
    assert not staging_dir.exists()


def test_main_fails_before_staging_when_pypi_query_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    _write_reviewed_artifacts(artifact_dir)
    staging_dir = tmp_path / "publish"
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def unavailable(project: str, version: str) -> dict[str, str] | None:
        raise ReleaseStateError("PyPI query failed")

    monkeypatch.setattr("scripts.pypi_release_guard.fetch_remote_hashes", unavailable)

    result = main(
        [
            "--project",
            "package",
            "--version",
            "1.0",
            "--artifact-dir",
            str(artifact_dir),
            "--staging-dir",
            str(staging_dir),
        ]
    )

    assert result == 1
    assert not output.exists()
    assert not staging_dir.exists()


def test_github_output_is_exact_boolean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    write_github_output(True)
    write_github_output(False)
    assert output.read_text(encoding="utf-8") == "publish=true\npublish=false\n"


def test_publish_workflow_stages_each_package_before_upload() -> None:
    workflow = (Path(__file__).parents[3] / ".github/workflows/publish.yml").read_text(
        encoding="utf-8"
    )
    assert workflow.count("python scripts/pypi_release_guard.py") == 3
    assert workflow.count("if: steps.release-state.outputs.publish == 'true'") == 3
    for project, artifact_dir, staging_dir in (
        ("cognifity-verdict", "dist/core", "dist/publish/core"),
        ("cognifity-verdict-eval", "dist/eval", "dist/publish/eval"),
        ("cognifity-verdict-inspect", "dist/inspect", "dist/publish/inspect"),
    ):
        assert f"--project {project}" in workflow
        assert f"--artifact-dir {artifact_dir}" in workflow
        assert f"--staging-dir {staging_dir}" in workflow
        assert f"packages-dir: {staging_dir}/" in workflow
