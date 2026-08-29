from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

openai = pytest.importorskip("openai")


class _OpenAIHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(length))
        model = request["model"]
        content = (
            "short useful answer" if model == "model-a" else "I cannot comply. " + "verbose " * 120
        )
        payload = json.dumps(
            {
                "id": f"completion-{model}",
                "object": "chat.completion",
                "created": 1_787_000_000,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def test_real_sdk_capture_then_matched_count_analysis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import verdict
    from verdict import client as verdict_client
    from verdict.dashboard.app import build_bundle
    from verdict.storage import SQLiteStorage
    from verdict_eval.cli.monitor import main as monitor_main

    server = ThreadingHTTPServer(("127.0.0.1", 0), _OpenAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    db = tmp_path / "verdict.db"
    try:
        verdict.init(
            storage=f"sqlite:///{db}",
            capture_content=True,
            instrumentors=["openai"],
        )
        client = openai.OpenAI(
            api_key="synthetic-test-key",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
        )
        for index in range(10):
            for model in ("model-a", "model-b"):
                verdict.set_context(
                    session_id=f"{model}-session-{index}",
                    workload="matched-poc",
                )
                with verdict.intent_context(f"prompt-{index}"):
                    client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": "summarize the incident report"}],
                    )
        verdict_client.shutdown()
    finally:
        verdict_client.shutdown()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    storage = SQLiteStorage(str(db))
    try:
        traces = storage.list_traces(limit=100)
    finally:
        storage.close()
    assert len(traces) == 20
    assert all(trace.prompt_redacted and trace.response_redacted for trace in traces)

    assert (
        monitor_main(
            [
                "--storage",
                f"sqlite:///{db}",
                "matched",
                "--baseline-model",
                "model-a",
                "--current-model",
                "model-b",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["matched_pairs"] == 10
    assert payload["status"] == "drift_detected"
    assert payload["persisted_run_id"]
    dashboard = build_bundle(db)
    assert dashboard["driftAnalysis"]["runStatus"] == "completed_with_signals"
    assert dashboard["driftSignals"][0]["statName"] == "wilcoxon_paired"
    assert dashboard["driftSignals"][0]["layers"] == ["matched_structural"]
