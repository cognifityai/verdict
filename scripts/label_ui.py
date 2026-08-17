"""Tiny local labeling UI for the binary-rubric harness — no dependencies.

Serves a one-at-a-time labeling page on localhost so you can PASS/FAIL each
rubric dimension from dropdowns instead of hand-editing JSONL. Autosaves every
change back to the file, so you can stop and resume anytime.

Usage:
    python scripts/label_ui.py --file labels.jsonl          # then open the URL
    python scripts/label_ui.py --file labels.jsonl --port 8123

Works on a labels.jsonl made by `verify_rubric_alignment.py --make-template`
(rows with a "labels" object). Also accepts a raw.jsonl (no labels) and adds
empty label slots. When you're done, run:
    python scripts/verify_rubric_alignment.py --labeled labels.jsonl --provider ...

Binds to 127.0.0.1 only (local machine). stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMENSIONS = ["groundedness", "relevance", "completeness", "safety", "instruction_following"]

FILE_PATH = ""


def _load(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            labels = obj.get("labels") or {}
            obj["labels"] = {d: labels.get(d) for d in DIMENSIONS}
            rows.append(obj)
    return rows


def _save(path: str, rows: list[dict]) -> None:
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps({
                "query": r.get("query", ""),
                "response": r.get("response", ""),
                "context": r.get("context", ""),
                "labels": {d: (r.get("labels") or {}).get(d) for d in DIMENSIONS},
            }) + "\n")


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Verdict labeling</title>
<style>
 body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 .wrap{max-width:900px;margin:0 auto;padding:20px}
 .bar{position:sticky;top:0;background:#0f1115;padding:10px 0;border-bottom:1px solid #2a2f3a;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 .prog{flex:1;height:8px;background:#242a35;border-radius:6px;overflow:hidden}
 .prog>i{display:block;height:100%;background:#4ea1ff}
 button{font:inherit;background:#242a35;color:#e6e6e6;border:1px solid #3a4150;border-radius:8px;padding:8px 14px;cursor:pointer}
 button:hover{background:#2e3644}
 .q{background:#171a21;border:1px solid #2a2f3a;border-radius:10px;padding:14px;margin:14px 0;white-space:pre-wrap}
 .r{background:#12161d;border:1px solid #2a2f3a;border-radius:10px;padding:14px;margin:14px 0;white-space:pre-wrap;max-height:340px;overflow:auto}
 label{display:inline-block;min-width:180px;color:#9aa4b2}
 .dim{margin:8px 0}
 select{font:inherit;background:#242a35;color:#e6e6e6;border:1px solid #3a4150;border-radius:8px;padding:6px 10px}
 select.PASS{border-color:#2f9e5e;color:#7ee0a5} select.FAIL{border-color:#c0504d;color:#ff9a97}
 .tag{font-size:12px;color:#7a8494} h2{font-size:14px;color:#9aa4b2;margin:18px 0 4px}
 .save{font-size:12px;color:#6fbf73;min-width:70px}
</style></head><body><div class="wrap">
<div class="bar">
 <button onclick="go(-1)">← Prev</button>
 <button onclick="go(1)">Next →</button>
 <span class="tag" id="pos"></span>
 <div class="prog"><i id="progbar"></i></div>
 <span class="tag" id="count"></span>
 <span class="save" id="save"></span>
</div>
<div id="card"></div>
</div>
<script>
const DIMS=__DIMS__; let DATA=__DATA__; let i=0;
function labeledCount(){let n=0;for(const r of DATA){if(DIMS.some(d=>r.labels[d]==="PASS"||r.labels[d]==="FAIL"))n++;}return n;}
function render(){
 const r=DATA[i];
 document.getElementById('pos').textContent=`Example ${i+1} / ${DATA.length}`;
 document.getElementById('count').textContent=`${labeledCount()} labeled`;
 document.getElementById('progbar').style.width=(100*(i+1)/DATA.length)+'%';
 let h=`<div class="q"><b>QUERY</b>\\n${esc(r.query)}</div>`;
 if(r.context) h+=`<div class="q"><b>CONTEXT</b>\\n${esc(r.context)}</div>`;
 h+=`<div class="r"><b>RESPONSE</b>\\n${esc(r.response)}</div><h2>Your PASS / FAIL</h2>`;
 for(const d of DIMS){
   const v=r.labels[d]||'';
   h+=`<div class="dim"><label>${d}</label>
     <select class="${v}" onchange="setLabel('${d}',this.value)">
       <option value="" ${v===''?'selected':''}>— skip</option>
       <option value="PASS" ${v==='PASS'?'selected':''}>PASS</option>
       <option value="FAIL" ${v==='FAIL'?'selected':''}>FAIL</option>
     </select></div>`;
 }
 document.getElementById('card').innerHTML=h;
}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');}
function setLabel(d,v){DATA[i].labels[d]=v||null;render();save();}
function go(n){i=Math.max(0,Math.min(DATA.length-1,i+n));render();}
let t=null;
function save(){document.getElementById('save').textContent='saving…';
 clearTimeout(t);t=setTimeout(()=>{fetch('/save',{method:'POST',body:JSON.stringify(DATA)})
   .then(()=>document.getElementById('save').textContent='saved ✓')
   .catch(()=>document.getElementById('save').textContent='SAVE FAILED');},200);}
document.addEventListener('keydown',e=>{if(e.key==='ArrowRight')go(1);if(e.key==='ArrowLeft')go(-1);});
render();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        rows = _load(FILE_PATH)
        data = json.dumps(rows).replace("</", "<\\/")
        html = _PAGE.replace("__DATA__", data).replace("__DIMS__", json.dumps(DIMENSIONS))
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/save":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            rows = json.loads(self.rfile.read(n).decode("utf-8"))
            _save(FILE_PATH, rows)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())


def main() -> int:
    global FILE_PATH
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", required=True, help="labels.jsonl (or raw.jsonl) to label.")
    p.add_argument("--port", type=int, default=8123)
    args = p.parse_args()
    FILE_PATH = args.file
    try:
        rows = _load(FILE_PATH)
    except OSError as e:
        print(f"Could not read {FILE_PATH!r}: {e}")
        return 1
    _save(FILE_PATH, rows)  # normalize (adds label slots if it was raw.jsonl)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Labeling {len(rows)} examples from {FILE_PATH}")
    print(f"Open {url} in your browser. Autosaves on every change. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Your labels are saved in", FILE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
