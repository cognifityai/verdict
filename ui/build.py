"""Generate local-asset HTML shells for the Verdict UI.

The JavaScript and CSS are compiled separately by ``pnpm build``. The generated
pages intentionally contain no captured data, inline scripts, CDN references,
or browser-side JSX compiler.

Run from the repository root:

    pnpm --dir ui install --frozen-lockfile
    pnpm --dir ui build
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

PAGES = {
    "landing.html": ("Verdict - LLM Observability", "landing.js"),
    "dashboard.html": ("Verdict - Dashboard", "dashboard.js"),
    "VerdictUI.html": ("Verdict - LLM Observability", "all-in-one.js"),
}


def page(title: str, script: str) -> str:
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="dark" />
<title>{title}</title>
<link rel="stylesheet" href="assets/verdict.css" />
</head>
<body>
<div id="root"><div id="loading">Loading...</div></div>
<script type="module" src="assets/{script}"></script>
</body>
</html>
'''


def main() -> None:
    for filename, (title, script) in PAGES.items():
        (HERE / filename).write_text(page(title, script))
    print("Wrote landing.html, dashboard.html, VerdictUI.html")


if __name__ == "__main__":
    main()
