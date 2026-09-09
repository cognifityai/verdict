"""Minimal full-agent capture example using Verdict's local file transport."""

from pathlib import Path

import verdict


def answer_question(question: str) -> str:
    """Stand-in for an application call; supported provider SDK calls auto-link."""
    return f"Received: {question}"


verdict.init(
    transport="file",
    spool_directory=Path("./verdict-capture"),
    service_name="example-agent",
    environment="development",
    instrumentors=[],
)

with verdict.agent_run(name="example-agent", session_id="example-session") as run:
    with run.turn(user_input="Check the service") as turn:
        with turn.tool("health_check", arguments={"service": "api"}) as tool:
            tool.set_output({"healthy": True})
        turn.record_test(command="pytest -q", exit_code=0, passed=12)
        turn.set_output(answer_question("Check the service"))
    run.record_business_outcome("request_resolved", True)

verdict.shutdown()
