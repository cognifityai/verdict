from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = Path(
    os.environ.get(
        "VERDICT_SKILL_SCANNER",
        SKILL_ROOT / "scripts" / "scan_repository.py",
    )
)

SPEC = importlib.util.spec_from_file_location("verdict_skill_scanner", SCANNER_PATH)
assert SPEC is not None and SPEC.loader is not None
SCANNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCANNER
SPEC.loader.exec_module(SCANNER)


class ScannerTests(unittest.TestCase):
    def create_repo(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        return temporary, root

    def test_classifies_released_python_and_unsupported_paths(self) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        (root / "app.py").write_text(
            "client.chat.completions.create(model='mock')\n"
            "client.chat.completions.stream(model='mock')\n"
            "client.responses.create(model='mock')\n"
            "client.responses.stream(model='mock')\n"
            "anthropic.messages.stream(model='mock')\n"
            "google.models.generate_content(model='mock', contents='x')\n"
            "legacy_model.generate_content('x')\n"
            "verdict.init(capture_content=True, buffered_writes=True)\n"
            "verdict.shutdown()\n"
            "from verdict.client import shutdown\n",
            encoding="utf-8",
        )
        (root / "web.ts").write_text(
            "await client.chat.completions.create({ model: 'mock' });",
            encoding="utf-8",
        )

        report = SCANNER.scan(root, 100)
        by_rule_and_path = {
            (item["rule_id"], item["path"]): item for item in report["findings"]
        }

        self.assertEqual(
            by_rule_and_path[("openai-chat-completions-create", "app.py")]["support"],
            "supported",
        )
        self.assertEqual(
            by_rule_and_path[("openai-chat-completions-stream", "app.py")][
                "support"
            ],
            "supported-with-constraints",
        )
        self.assertEqual(
            by_rule_and_path[("openai-chat-completions-create", "web.ts")]["support"],
            "unsupported",
        )
        self.assertEqual(
            by_rule_and_path[("openai-responses-create", "app.py")]["support"],
            "unsupported",
        )
        self.assertEqual(
            by_rule_and_path[("openai-responses-stream", "app.py")]["support"],
            "unsupported",
        )
        self.assertEqual(
            by_rule_and_path[("anthropic-messages-stream", "app.py")]["support"],
            "unsupported",
        )
        self.assertEqual(
            by_rule_and_path[("google-generate-content", "app.py")]["support"],
            "supported",
        )
        self.assertEqual(
            by_rule_and_path[("google-legacy-generate-content", "app.py")]["support"],
            "supported-with-constraints",
        )
        self.assertIn(("content-capture-enabled", "app.py"), by_rule_and_path)
        self.assertIn(("buffered-writes-enabled", "app.py"), by_rule_and_path)
        self.assertEqual(
            by_rule_and_path[("verdict-shutdown-not-exported", "app.py")]["support"],
            "conflict",
        )
        self.assertIn(("verdict-client-shutdown-import", "app.py"), by_rule_and_path)

    def test_ignores_docs_generated_trees_and_never_emits_source_values(self) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        canary = "sk-never-emit-this-canary"
        (root / "README.md").write_text(
            "Example: client.chat.completions.create(...)", encoding="utf-8"
        )
        (root / "app.py").write_text(
            f"'''verdict.init() and client.responses.create() are examples only.'''\n"
            f"# client.messages.stream() is not executable\n"
            f"secret = '{canary}'\n"
            "example = 'client.chat.completions.stream()'\n"
            "client.chat.completions.create(model='mock')\n",
            encoding="utf-8",
        )
        ignored = root / "node_modules"
        ignored.mkdir()
        (ignored / "fake.ts").write_text(
            "client.chat.completions.create({model: 'ignored'})", encoding="utf-8"
        )

        report = SCANNER.scan(root, 100)
        rendered = json.dumps(report)

        self.assertNotIn(canary, rendered)
        self.assertFalse(
            any(item["path"] == "README.md" for item in report["findings"])
        )
        self.assertFalse(
            any(item["path"].startswith("node_modules/") for item in report["findings"])
        )
        self.assertFalse(
            any(
                item["rule_id"] == "wrong-verdict-distribution"
                for item in report["findings"]
            )
        )
        self.assertEqual(report["scanned_files"], 1)
        self.assertEqual(report["python_parse_fallback_files"], 0)
        self.assertEqual(
            [item["rule_id"] for item in report["findings"]],
            ["openai-chat-completions-create"],
        )

    def test_reports_dependency_scheduler_and_storage_risks_without_uri_values(
        self,
    ) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        secret_uri = "postgresql://user:do-not-emit@db.example/verdict"
        (root / "requirements.txt").write_text(
            "verdict>=1\ncognifity-verdict==0.1.0a4\n", encoding="utf-8"
        )
        (root / "requirements-dev.txt").write_text("verdict==3\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            '[project]\ndependencies = ["verdict>=2"]\n', encoding="utf-8"
        )
        (root / "deploy.yaml").write_text(
            "\n".join(
                [
                    "schedule: '0 * * * *'",
                    "relative: sqlite:///data/verdict.db",
                    "absolute: sqlite:////var/lib/verdict.db",
                    "windows: sqlite:///C:/verdict/data.db",
                    f"shared: {secret_uri}",
                    "command: python scripts/run_drift_pipeline.py --db data/verdict.db",
                    "bad: python scripts/run_drift_pipeline.py --yes-spend --max-spend-usd=1",
                ]
            ),
            encoding="utf-8",
        )

        report = SCANNER.scan(root, 100)
        rule_ids = [item["rule_id"] for item in report["findings"]]
        rendered = json.dumps(report)

        self.assertEqual(rule_ids.count("wrong-verdict-distribution"), 3)
        self.assertIn("cognifity-verdict-distribution", rule_ids)
        self.assertIn("scheduler-config", rule_ids)
        self.assertEqual(rule_ids.count("relative-sqlite-uri"), 1)
        self.assertIn("postgres-storage-uri", rule_ids)
        self.assertIn("drift-pipeline-db-flag", rule_ids)
        budget_finding = next(
            item
            for item in report["findings"]
            if item["rule_id"] == "unsupported-drift-budget-flag"
        )
        self.assertEqual(budget_finding["support"], "conflict")
        self.assertNotIn(secret_uri, rendered)

    def test_cli_json_and_error_contracts(self) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        (root / "app.py").write_text(
            "client.chat.completions.create(model='mock')", encoding="utf-8"
        )

        success = subprocess.run(
            [sys.executable, str(SCANNER_PATH), str(root), "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["schema_version"], 1)

        missing = subprocess.run(
            [sys.executable, str(SCANNER_PATH), str(root / "missing")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("not a directory", missing.stderr)

        invalid_limit = subprocess.run(
            [sys.executable, str(SCANNER_PATH), str(root), "--max-files", "0"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(invalid_limit.returncode, 2)
        self.assertIn("must be positive", invalid_limit.stderr)

    def test_file_limit_fails_closed(self) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        (root / "a.py").write_text("pass", encoding="utf-8")
        (root / "b.py").write_text("pass", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(SCANNER_PATH), str(root), "--max-files", "1"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("scan exceeded", result.stderr)

    def test_finding_limit_fails_closed(self) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        (root / "app.py").write_text(
            "\n".join(
                "client.chat.completions.create(model='mock')" for _ in range(3)
            ),
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(SCANNER_PATH),
                str(root),
                "--max-findings",
                "2",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--max-findings=2", result.stderr)

    def test_multiline_shell_yaml_and_array_commands_are_checked(self) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        (root / "schedule.sh").write_text(
            "python /opt/verdict/scripts/run_drift_pipeline.py \\\n"
            "  --storage sqlite:////tmp/verdict.db \\\n"
            "  --db /tmp/verdict.db \\\n"
            "  --yes-spend\n"
            "python /opt/verdict/ui/server.py --db /tmp/verdict.db\n"
            "args=(\n"
            "  python\n"
            "  /opt/verdict/scripts/run_drift_pipeline.py\n"
            "  --max-spend-usd=1\n"
            ")\n",
            encoding="utf-8",
        )
        (root / "workflow.yaml").write_text(
            "command: >\n"
            "  python /opt/verdict/scripts/run_drift_pipeline.py\n"
            "  --db /tmp/verdict.db\n"
            "args:\n"
            "  - python\n"
            "  - /opt/verdict/scripts/run_drift_pipeline.py\n"
            "  - --max-spend-usd=2\n",
            encoding="utf-8",
        )

        report = SCANNER.scan(root, 100)
        conflicts = [
            (item["rule_id"], item["path"]) for item in report["findings"]
            if item["support"] == "conflict"
        ]

        self.assertEqual(conflicts.count(("drift-pipeline-db-flag", "schedule.sh")), 1)
        self.assertEqual(
            conflicts.count(("unsupported-drift-budget-flag", "schedule.sh")), 2
        )
        self.assertEqual(conflicts.count(("drift-pipeline-db-flag", "workflow.yaml")), 1)
        self.assertEqual(
            conflicts.count(("unsupported-drift-budget-flag", "workflow.yaml")), 1
        )

    def test_malformed_python_reports_fallback_without_emitting_source(self) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        canary = "private-malformed-source-canary"
        (root / "broken.py").write_text(
            f"secret = '{canary}'\nif (\nclient.responses.create()\n",
            encoding="utf-8",
        )

        report = SCANNER.scan(root, 100)

        self.assertEqual(report["python_parse_fallback_files"], 1)
        self.assertNotIn(canary, json.dumps(report))
        fallback = next(
            item
            for item in report["findings"]
            if item["rule_id"] == "python-parse-fallback"
        )
        self.assertEqual(fallback["path"], "broken.py")
        self.assertEqual(fallback["support"], "review")

    def test_skips_large_symlinked_and_non_regular_files(self) -> None:
        temporary, root = self.create_repo()
        self.addCleanup(temporary.cleanup)
        canary = "client.chat.completions.create(model='must-not-match')"
        (root / "large.py").write_bytes(
            canary.encode("utf-8") + b"x" * (SCANNER.MAX_FILE_BYTES + 1)
        )
        target = root / "target.py"
        target.write_text(canary, encoding="utf-8")
        (root / "linked.py").symlink_to(target)

        fifo = root / "requirements.txt"
        if hasattr(os, "mkfifo"):
            os.mkfifo(fifo)

        report = SCANNER.scan(root, 100)
        paths = {item["path"] for item in report["findings"]}

        self.assertEqual(report["skipped_large_files"], 1)
        self.assertEqual(paths, {"target.py"})


if __name__ == "__main__":
    unittest.main()
