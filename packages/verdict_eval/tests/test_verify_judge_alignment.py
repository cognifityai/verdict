from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_judge_alignment.py"
SWEEP_SCRIPT = SCRIPT.with_name("run_alignment_sweep.py")
SPEC = importlib.util.spec_from_file_location("verify_judge_alignment", SCRIPT)
assert SPEC is not None
verifier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verifier)


def _mtbench_row() -> dict:
    return {
        "conversation_a": [
            {"role": "user", "content": "Turn 1 question"},
            {"role": "assistant", "content": "Assistant A turn 1 answer"},
            {"role": "user", "content": "Turn 2 follow-up"},
            {"role": "assistant", "content": "Assistant A turn 2 answer"},
        ],
        "conversation_b": [
            {"role": "user", "content": "Turn 1 question"},
            {"role": "assistant", "content": "Assistant B turn 1 answer"},
            {"role": "user", "content": "Turn 2 follow-up"},
            {"role": "assistant", "content": "Assistant B turn 2 answer"},
        ],
    }


def test_full_context_pair_preserves_all_mtbench_turns() -> None:
    query, response_a, response_b = verifier._build_full_context_pair(_mtbench_row())

    assert "Turn 1 question" in query
    assert "Turn 2 follow-up" in query
    assert "Assistant A turn 1 answer" in response_a
    assert "Assistant A turn 2 answer" in response_a
    assert "Assistant B turn 1 answer" in response_b
    assert "Assistant B turn 2 answer" in response_b


def test_legacy_pair_reproduces_first_user_final_answer_extraction() -> None:
    query, response_a, response_b = verifier._build_legacy_pair(_mtbench_row())

    assert query == "Turn 1 question"
    assert response_a == "Assistant A turn 2 answer"
    assert response_b == "Assistant B turn 2 answer"


def test_offline_cli_writes_machine_readable_headline_metrics(tmp_path) -> None:
    output = tmp_path / "alignment.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "offline",
            "--provider",
            "anthropic",
            "--judge-model",
            "fixture-judge",
            "--json-output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["schemaVersion"] == 1
    assert report["dataset"] == {"name": "synthetic", "revision": None}
    assert report["judge"] == {
        "provider": "anthropic",
        "model": "fixture-judge",
    }
    assert isinstance(report["metrics"]["threeWay"]["cohensKappa"], float)
    assert isinstance(report["metrics"]["binarized"]["cohensKappa"], float)
    assert isinstance(report["metrics"]["nonTieAgreement"], float)
    assert isinstance(report["metrics"]["inconsistentCount"], int)


def test_online_dataset_revision_is_an_immutable_commit() -> None:
    revision = verifier.MT_BENCH_DATASET_REVISION

    assert len(revision) == 40
    assert all(character in "0123456789abcdef" for character in revision)


def test_alignment_sweep_propagates_when_every_verifier_run_fails(tmp_path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    sweep = scripts / "run_alignment_sweep.sh"
    sweep.write_text((SCRIPT.parent / "run_alignment_sweep.sh").read_text())
    sweep.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nexit 9\n")
    fake_python.chmod(0o755)
    output = tmp_path / "results"
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["ALIGNMENT_OUT"] = str(output)

    result = subprocess.run(
        ["bash", str(sweep)],
        cwd=project,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0


def test_alignment_sweep_builds_complete_summary_from_json(tmp_path) -> None:
    output = tmp_path / "offline-sweep"

    result = subprocess.run(
        [
            sys.executable,
            str(SWEEP_SCRIPT),
            "--mode",
            "offline",
            "--n",
            "5",
            "--out",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    summary = (output / "SUMMARY.md").read_text()
    assert summary.count("| anthropic ::") == 2
    assert summary.count("| openai ::") == 1
    assert summary.count("| google ::") == 1
    assert "MISSING" not in summary
    assert "PARSE-FAIL" not in summary
    assert "RUN-FAIL" not in summary
    assert "Requested pairs per judge" not in summary
    assert "120 fixed synthetic pairs per judge" in summary
    assert summary.count("SYNTHETIC WIRING ONLY") == 4
    assert "95% CI" in summary
    assert "n scored/available" in summary
    for label in ("01_haiku", "02_gpt4omini", "03_gemini", "04_sonnet"):
        report = json.loads((output / f"{label}.json").read_text())
        assert isinstance(report["metrics"]["threeWay"]["cohensKappa"], float)
        assert report["verdict"]["status"] == "synthetic"


def test_alignment_sweep_rejects_incomplete_online_report(tmp_path) -> None:
    fake_verifier = tmp_path / "partial_verifier.py"
    fake_verifier.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "provider = sys.argv[sys.argv.index('--provider') + 1]\n"
        "model = sys.argv[sys.argv.index('--judge-model') + 1]\n"
        "output = Path(sys.argv[sys.argv.index('--json-output') + 1])\n"
        "report = {\n"
        "  'schemaVersion': 1,\n"
        "  'mode': 'online',\n"
        "  'dataset': {'name': 'lmsys/mt_bench_human_judgments', "
        "'revision': 'f7d2896d2cc5d80f8b55c2bbc722613555233c25'},\n"
        "  'judge': {'provider': provider, 'model': model},\n"
        "  'contextMode': 'full',\n"
        "  'pairs': {'available': 50, 'scored': 1},\n"
        "  'verdict': {'status': 'acceptable', 'message': 'not enough evidence'},\n"
        "  'metrics': {\n"
        "    'threeWay': {'cohensKappa': 1.0, 'gwetsAc2': 1.0, 'gwetsAc2Ci95': [1.0, 1.0]},\n"
        "    'binarized': {'pairsKept': 1, 'cohensKappa': 1.0, 'cohensKappaCi95': [1.0, 1.0], 'gwetsAc2': 1.0, 'gwetsAc2Ci95': [1.0, 1.0]},\n"
        "    'nonTieAgreement': 1.0, 'inconsistentCount': 0\n"
        "  }\n"
        "}\n"
        "output.write_text(json.dumps(report))\n"
    )
    output = tmp_path / "partial-sweep"

    result = subprocess.run(
        [
            sys.executable,
            str(SWEEP_SCRIPT),
            "--mode",
            "online",
            "--n",
            "50",
            "--out",
            str(output),
            "--verifier",
            str(fake_verifier),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "scored 1 of 50" in (output / "SUMMARY.md").read_text()


def test_online_all_tie_judge_is_not_a_success(monkeypatch, tmp_path) -> None:
    from verdict_eval import pairwise, providers

    class Dataset(list):
        def shuffle(self, seed):
            assert seed == 42
            return self

        def select(self, indexes):
            return Dataset(self[index] for index in indexes)

    rows = Dataset([
        {**_mtbench_row(), "winner": "model_a", "category": "writing"}
        for _ in range(60)
    ])

    class TieJudge:
        def __init__(self, **_kwargs):
            pass

        def compare(self, **_kwargs):
            return SimpleNamespace(
                verdict=pairwise.PairwiseVerdict.TIE,
                component_judgments=[],
            )

    monkeypatch.setitem(
        sys.modules,
        "datasets",
        SimpleNamespace(load_dataset=lambda *_args, **_kwargs: rows),
    )
    monkeypatch.setattr(pairwise, "PairwiseJudge", TieJudge)
    monkeypatch.setattr(providers, "AnthropicAdapter", lambda: object())
    report_path = tmp_path / "unreliable.json"
    args = SimpleNamespace(
        provider="anthropic",
        judge_model="tie-judge",
        ensemble=False,
        context_mode="full",
        n=60,
        json_output=str(report_path),
    )

    returncode = verifier.run_online(args)

    assert returncode != 0
    report = json.loads(report_path.read_text())
    assert report["verdict"]["status"] == "unreliable"
    assert report["metrics"]["binarized"]["gwetsAc2Ci95"] is None


def test_online_partial_judge_failures_do_not_publish_a_report(monkeypatch, tmp_path) -> None:
    from verdict_eval import pairwise, providers

    class Dataset(list):
        def shuffle(self, seed):
            return self

        def select(self, indexes):
            return Dataset(self[index] for index in indexes)

    rows = Dataset([
        {**_mtbench_row(), "winner": "model_a", "category": "writing"}
        for _ in range(50)
    ])

    class MostlyFailingJudge:
        calls = 0

        def __init__(self, **_kwargs):
            pass

        def compare(self, **_kwargs):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("provider unavailable")
            return SimpleNamespace(
                verdict=pairwise.PairwiseVerdict.A_BETTER,
                component_judgments=[],
            )

    monkeypatch.setitem(
        sys.modules,
        "datasets",
        SimpleNamespace(load_dataset=lambda *_args, **_kwargs: rows),
    )
    monkeypatch.setattr(pairwise, "PairwiseJudge", MostlyFailingJudge)
    monkeypatch.setattr(providers, "AnthropicAdapter", lambda: object())
    report_path = tmp_path / "partial.json"
    args = SimpleNamespace(
        provider="anthropic",
        judge_model="mostly-failing-judge",
        ensemble=False,
        context_mode="full",
        n=50,
        json_output=str(report_path),
    )

    assert verifier.run_online(args) != 0
    assert not report_path.exists()


def test_online_cli_rejects_sample_below_minimum(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--mode", "online", "--n", "49"],
    )
    monkeypatch.setattr(verifier, "run_online", lambda _args: 0)

    with pytest.raises(SystemExit) as exc_info:
        verifier.main()

    assert exc_info.value.code == 2


def test_alignment_sweep_marks_invalid_json_and_exits_nonzero(tmp_path) -> None:
    verifier = tmp_path / "invalid_verifier.py"
    verifier.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "p = Path(sys.argv[sys.argv.index('--json-output') + 1])\n"
        "p.write_text('{}')\n"
    )
    output = tmp_path / "invalid-sweep"

    result = subprocess.run(
        [
            sys.executable,
            str(SWEEP_SCRIPT),
            "--mode",
            "offline",
            "--n",
            "5",
            "--out",
            str(output),
            "--verifier",
            str(verifier),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    summary = (output / "SUMMARY.md").read_text()
    assert "PARSE-FAIL" in summary
    assert "| — |" not in summary


def test_alignment_sweep_does_not_reuse_stale_result_json(tmp_path) -> None:
    verifier = tmp_path / "silent_verifier.py"
    verifier.write_text("# exits zero without writing --json-output\n")
    output = tmp_path / "stale-sweep"
    output.mkdir()
    stale = {
        "schemaVersion": 1,
        "judge": {"provider": "anthropic", "model": "claude-haiku-4-5"},
        "metrics": {
            "threeWay": {"cohensKappa": 1.0},
            "binarized": {"cohensKappa": 1.0},
            "nonTieAgreement": 1.0,
            "inconsistentCount": 0,
        },
    }
    (output / "01_haiku.json").write_text(json.dumps(stale))

    result = subprocess.run(
        [
            sys.executable,
            str(SWEEP_SCRIPT),
            "--mode",
            "offline",
            "--n",
            "5",
            "--out",
            str(output),
            "--verifier",
            str(verifier),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "PARSE-FAIL" in (output / "SUMMARY.md").read_text()
