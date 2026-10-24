# Contributing to Verdict

Thank you for helping improve Verdict. This project is an early public beta,
so focused bug fixes, tests, documentation corrections, and narrowly scoped
features are especially useful.

## Before You Start

- Search existing issues and pull requests before opening a duplicate.
- Open an issue before beginning a large feature or architectural change.
- Keep changes focused on the documented application-agent and model-call workflows.
  Agent-run relationships require explicit recorded links; a captured final answer
  alone does not establish task success.
- Report security vulnerabilities privately as described in
  [SECURITY.md](SECURITY.md).

## Development Setup

Verdict requires Python 3.10 or newer. From the repository root:

```bash
uv venv --python 3.12
source .venv/bin/activate
pip install -e "packages/verdict[anthropic,openai,google,dashboard]"
pip install -e packages/verdict_eval
pip install -e packages/verdict_inspect
pip install pytest pytest-asyncio ruff
```

While editing, run the tests for the changed package or workflow. Run the full
suite once for the final pull request candidate, along with the key-free smoke
check:

```bash
python scripts/smoke_test.py
python -m pytest -q
```

Run `ruff check <changed Python files>` for Python files you modify.

The dashboard ships checked-in production assets and does not need Node at
runtime. When changing files under `ui/`, install Node 18 or newer and pnpm,
then rebuild the tracked assets:

```bash
pnpm --dir ui install --frozen-lockfile
pnpm --dir ui build
python -m pytest -q ui/tests
```

Include the updated `ui/landing.html`, `ui/assets/`, and packaged dashboard
assets under `packages/verdict/src/verdict/dashboard/static/` in the same pull
request as the source change.

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
