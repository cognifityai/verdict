"""Generate local-asset HTML shells for the Verdict UI.

The JavaScript and CSS are compiled separately by ``pnpm build``. The generated
pages intentionally contain no captured data, inline scripts, CDN references,
or browser-side JSX compiler.

Run from the repository root:

    pnpm --dir ui install --frozen-lockfile
    pnpm --dir ui build
"""
from __future__ import annotations

import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASHBOARD = (
    HERE.parent
    / "packages"
    / "verdict"
    / "src"
    / "verdict"
    / "dashboard"
    / "static"
)

PAGES = {
    "landing.html": ("Verdict - LLM Observability", "landing.js"),
    "VerdictUI.html": ("Verdict - LLM Observability", "all-in-one.js"),
}


def asset_url(filename: str, root: Path = HERE) -> str:
    digest = hashlib.sha256((root / "assets" / filename).read_bytes()).hexdigest()[:12]
    return f"assets/{filename}?v={digest}"


def page(title: str, script: str, root: Path = HERE) -> str:
    stylesheet_url = asset_url("verdict.css", root)
    script_url = asset_url(script, root)
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="dark" />
<title>{title}</title>
<link rel="stylesheet" href="{stylesheet_url}" />
</head>
<body>
<div id="root"><div id="loading">Loading...</div></div>
<script type="module" src="{script_url}"></script>
</body>
</html>
'''


def main() -> None:
    for filename, (title, script) in PAGES.items():
        (HERE / filename).write_text(page(title, script))
    (DASHBOARD / "dashboard.html").write_text(
        page("Verdict - Dashboard", "dashboard.js", DASHBOARD)
    )
    print("Wrote landing.html, VerdictUI.html, and packaged dashboard.html")


if __name__ == "__main__":
    main()
