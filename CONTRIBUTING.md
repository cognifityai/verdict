# Contributing to Verdict

Thank you for helping improve Verdict. This project is an early public alpha,
so focused bug fixes, tests, documentation corrections, and narrowly scoped
features are especially useful.

## Before You Start

- Search existing issues and pull requests before opening a duplicate.
- Open an issue before beginning a large feature or architectural change.
- Keep changes within Verdict's current individual-LLM-call scope. Agent-run
  graphs, tool-sequence analysis, and task-success evaluation are roadmap work.
- Report security vulnerabilities privately as described in
  [SECURITY.md](SECURITY.md).

## Development Setup

Verdict requires Python 3.10 or newer. From the repository root:

```bash
uv venv --python 3.12
source .venv/bin/activate
pip install -e "packages/verdict[anthropic,openai,google]"
pip install -e packages/verdict_eval
pip install -e packages/verdict_inspect
pip install -r ui/requirements.txt
pip install pytest pytest-asyncio ruff
```

Run the key-free checks:

```bash
python scripts/smoke_test.py
python -m pytest -q
```

Run `ruff check <changed Python files>` for Python files you modify.

Provider instrumentation changes should also be checked with
`scripts/live_capture_check.py` against the real SDKs they affect. That script
makes paid provider calls, so use only providers for which you have configured
credentials.

## Pull Requests

- Keep each pull request focused on one coherent change.
- Add or update tests for behavioral changes.
- Update documentation when public behavior, installation, or limitations
  change.
- Describe what you verified and what you could not run.
- Do not report a proxy or mocked path as verification of a real provider path.
- Do not include generated databases, traces, reports, credentials, customer
  data, personal data, presentation files, or local environment files.

By submitting a contribution, you agree that it is licensed under the Apache
License 2.0 that covers this repository.
