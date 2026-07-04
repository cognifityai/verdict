"""Build the served HTML pages from VerdictUI.jsx.

Produces three self-contained, CDN-backed HTML files:

  landing.html    Public marketing page only. No captured data, no dashboard
                  code. Its CTA links to /dashboard.
  dashboard.html  The observability SPA + the embedded snapshot + the live
                  /api/data fetch. Served behind the password gate.
  VerdictUI.html  All-in-one (landing + dashboard) for opening locally without
                  a server; falls back to the embedded snapshot when no API.

The dashboard pieces and the data snapshot are deliberately kept OUT of
landing.html so the public page exposes nothing captured.

Run:  python ui/build.py
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = (HERE / "VerdictUI.jsx").read_text()

FONT = "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif"


def slice_between(start: str, end: str | None) -> str:
    a = SRC.index(start)
    b = SRC.index(end) if end else len(SRC)
    return SRC[a:b]


# Named source sections (see VerdictUI.jsx layout).
DATA = slice_between("const SEED =", "const C = {")            # SEED + API_URL + let DATA
PRELUDE = slice_between("const C = {", "function Landing(")    # palette, PROV, helpers
LANDING = slice_between("function Landing(", "function Dashboard(")  # Landing + Section
DASHBOARD = slice_between("function Dashboard(", "function App(")    # Dashboard + subviews
ALL_IN_ONE = SRC[SRC.index("const SEED ="):].replace(
    "export default function Root()", "function Root()")

ICONS = [x.strip() for x in re.search(r'import \{([^}]*)\} from "lucide-react";', SRC, re.S)
         .group(1).replace("\n", " ").split(",") if x.strip()]
RECHARTS = [x.strip() for x in re.search(r'import \{([^}]*)\} from "recharts";', SRC, re.S)
            .group(1).replace("\n", " ").split(",") if x.strip()]

LANDING_ROOT = f'''
function LandingRoot() {{
  return (
    <div style={{{{ fontFamily: "{FONT}", height: "100%", background: C.bg }}}}>
      <Landing onEnter={{() => {{ window.location.href = "/dashboard"; }}}} />
    </div>
  );
}}
const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(React.createElement(LandingRoot));
'''

DASHBOARD_ROOT = f'''
function DashboardRoot() {{
  const [source, setSource] = useState("sample");
  const [reloading, setReloading] = useState(false);
  const [, setVersion] = useState(0);
  const load = React.useCallback(() => {{
    setReloading(true);
    fetch(API_URL, {{ headers: {{ Accept: "application/json" }} }})
      .then((r) => {{ if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }})
      .then((d) => {{ if (d && d.meta && d.providers) {{ DATA = d; setSource("live"); setVersion((v) => v + 1); }} }})
      .catch(() => {{}})
      .finally(() => setReloading(false));
  }}, []);
  useEffect(() => {{ load(); }}, [load]);
  return (
    <div style={{{{ fontFamily: "{FONT}", height: "100%", background: C.bg }}}}>
      <style>{{"@keyframes vspin{{to{{transform:rotate(360deg)}}}}"}}</style>
      <Dashboard onExit={{() => {{ window.location.href = "/"; }}}} source={{source}} onReload={{load}} reloading={{reloading}} />
    </div>
  );
}}
const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(React.createElement(DashboardRoot));
'''

ALL_IN_ONE_ROOT = '''
const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(React.createElement(Root));
'''

CDN = {
    "tailwind": "https://cdn.tailwindcss.com",
    "react": "https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js",
    "react-dom": "https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js",
    "prop-types": "https://cdnjs.cloudflare.com/ajax/libs/prop-types/15.8.1/prop-types.min.js",
    "recharts": "https://cdnjs.cloudflare.com/ajax/libs/recharts/2.12.7/Recharts.min.js",
    "lucide": "https://cdnjs.cloudflare.com/ajax/libs/lucide/0.441.0/lucide.min.js",
    "babel": "https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.24.7/babel.min.js",
}

ICON_SHIM = '''
function makeIcon(name) {
  return function (props) {
    props = props || {};
    var size = props.size || 24;
    var L = window.lucide || {};
    var node = (L.icons && L.icons[name]) || L[name] || null;
    var kids = [];
    if (node) {
      var arr = Array.isArray(node) ? (Array.isArray(node[0]) ? node : (node.iconNode || [])) : (node.iconNode || []);
      kids = arr.map(function (c, i) { return React.createElement(c[0], Object.assign({ key: i }, c[1])); });
    }
    var rest = Object.assign({}, props); delete rest.size;
    return React.createElement("svg", Object.assign({
      width: size, height: size, viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round"
    }, rest), kids);
  };
}
'''


def page(title: str, body: str, include_recharts: bool) -> str:
    scripts = [CDN["tailwind"], CDN["react"], CDN["react-dom"]]
    if include_recharts:
        scripts += [CDN["prop-types"], CDN["recharts"]]
    scripts += [CDN["lucide"], CDN["babel"]]
    script_tags = "\n".join(f'<script src="{s}"></script>' for s in scripts)
    recharts_line = ("const { " + ", ".join(RECHARTS) + " } = Recharts;") if include_recharts else ""
    icon_consts = "\n".join(f'const {n} = makeIcon("{n}");' for n in ICONS)
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
{script_tags}
<style>
  html, body, #root {{ height: 100%; margin: 0; background: #0a0c10; }}
  #loading {{ color:#8b94a6; font-family: system-ui, sans-serif; display:flex; height:100%; align-items:center; justify-content:center; }}
</style>
</head>
<body>
<div id="root"><div id="loading">Loading…</div></div>
<script type="text/babel" data-presets="react">
const {{ useState, useMemo, useEffect }} = React;
{recharts_line}
{ICON_SHIM}
{icon_consts}

{body}
</script>
</body>
</html>
'''


def main():
    landing_body = PRELUDE + "\n" + LANDING + "\n" + LANDING_ROOT
    dashboard_body = PRELUDE + "\n" + DATA + "\n" + DASHBOARD + "\n" + DASHBOARD_ROOT
    all_body = ALL_IN_ONE + "\n" + ALL_IN_ONE_ROOT

    (HERE / "landing.html").write_text(page("Verdict — Agent Observability", landing_body, include_recharts=False))
    (HERE / "dashboard.html").write_text(page("Verdict — Dashboard", dashboard_body, include_recharts=True))
    (HERE / "VerdictUI.html").write_text(page("Verdict — Agent Observability", all_body, include_recharts=True))
    print("Wrote landing.html, dashboard.html, VerdictUI.html")


if __name__ == "__main__":
    main()
