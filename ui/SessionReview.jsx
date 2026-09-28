import React, {useEffect, useRef, useState} from "react";
const panel={borderColor:"#26332e",background:"#111715"};
const muted={color:"#94a39d"};

export function SessionReview({root, refresh=0}) {
  const [data,setData]=useState(null), [evaluator,setEvaluator]=useState(""), [detail,setDetail]=useState(null), [issue,setIssue]=useState(null), [error,setError]=useState(null);
  const detailEpoch=useRef(0);
  useEffect(() => {
    let current=true;
    detailEpoch.current++;
    setDetail(null);
    setData(null);
    setIssue(null);
    fetch(`${root}/api/data/sessions${evaluator ? `?evaluator=${encodeURIComponent(evaluator)}` : ""}`, {credentials:"same-origin"})
      .then(async r => {const d=await r.json();if(!r.ok)throw Error(d.error);return d;})
      .then(d => {if(current) {setData(d);setError(null);}}).catch(e=>{if(current)setError(String(e));});
    return()=>{current=false;detailEpoch.current++;};
  },[root,evaluator,refresh]);
  async function open(id) {
    const epoch=++detailEpoch.current;
    setDetail(null);
    setError(null);
    try {
      const r=await fetch(`${root}/api/data/sessions/${encodeURIComponent(id)}`,{credentials:"same-origin"});const d=await r.json();if(!r.ok)throw Error(d.error);if(epoch===detailEpoch.current)setDetail(d);
    }catch(e){if(epoch===detailEpoch.current)setError(String(e));}
  }
  const selected=evaluator || data?.selectedEvaluator;
  const rows=(data?.sessions || []).filter(s=>!issue || issue.sessionIds.includes(s.id));
  return <section className="border p-5 space-y-4" style={panel}>
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-lg font-semibold">Conversation evidence</h2><p className="text-sm mt-1" style={muted}>Inspect the original messages, scores and recurring issues. Select one rubric and judge version.</p></div>
      <select aria-label="Conversation evaluator" className="border p-2 bg-transparent text-sm max-w-full" value={selected || ""} onChange={e=>{detailEpoch.current++;setDetail(null);setEvaluator(e.target.value);setIssue(null);}}><option value="">Select an evaluator</option>{data?.evaluatorIdentities.map(e=><option key={e.fingerprint} value={e.fingerprint}>{e.rubric} v{e.version} · {e.target} · {e.model} · {e.source}</option>)}</select>
    </div>
    {data && <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">{[["Captured",data.coverage.captured],["Judged by this evaluator",data.coverage.judged],["Missing event time",data.coverage.untimed],["Unknown / open ending",data.coverage.unknownEnding]].map(([label,n])=><div key={label} className="border p-3" style={panel}><div className="text-xs" style={muted}>{label}</div><div className="text-xl mt-1">{n}</div></div>)}</div>}
    {!!data?.coverage.untimed && <p className="text-sm" style={{color:"#f2b84b"}}>Undated conversations can be reviewed and graded, but cannot establish time drift. Import explicit event timestamps and conversation endings for Monitor.</p>}
    {data?.issues.length > 0 && <div><h3 className="text-sm font-semibold">Recurring issues · unique conversations</h3><div className="flex flex-wrap gap-2 mt-2">{data.issues.map(i=><button key={i.issue} className="border p-2 text-sm" onClick={()=>setIssue(i)} style={panel}>{i.issue.replaceAll("_"," ")} · {i.conversations}</button>)}{issue && <button className="border p-2 text-sm" onClick={()=>setIssue(null)}>Clear filter</button>}</div></div>}
    <div className="overflow-auto max-h-96"><table className="w-full text-sm text-left"><thead><tr style={muted}><th className="p-2">Conversation</th><th className="p-2">Language / workflow</th><th className="p-2">Ending</th><th className="p-2">Evidence</th></tr></thead><tbody>{rows.map(s=><tr key={s.id} className="border-t" style={{borderColor:panel.borderColor}}><td className="p-2"><button className="underline" onClick={()=>open(s.id)}>{s.external_id || s.id.slice(0,12)}</button><div className="text-xs" style={muted}>{s.event_at ? new Date(s.event_at).toLocaleString() : "No event time"}</div></td><td className="p-2">{s.language || "Unknown"} / {s.workflow || "Unknown"}</td><td className="p-2">{s.end_status}</td><td className="p-2">{s.messageCount} messages · {s.assessments.filter(a=>a.status==="completed").length} current judgments{s.input_issues.length > 0 && <div style={{color:"#f2b84b"}}>{s.input_issues.join(" · ")}</div>}</td></tr>)}</tbody></table>{!rows.length && <p className="p-3 text-sm" style={muted}>No conversations here. Use Setup → Existing telemetry → Voice to import text transcripts.</p>}</div>
    {detail && <div className="border p-4 space-y-4" style={panel}><div className="flex justify-between"><h3 className="font-semibold">Messages and findings</h3><button className="underline text-sm" onClick={()=>{detailEpoch.current++;setDetail(null);}}>Close</button></div><p className="font-mono text-xs break-all" style={muted}>Revision {detail.session.revision}</p>
      {detail.assessments.filter(a=>a.session_revision===detail.session.revision && a.evaluator_fingerprint===selected).map(a=><div className="border p-3" style={panel} key={a.id}><strong>{a.rubric.name} v{a.rubric.version}</strong> · {a.rubric.target}{a.target_message_id && ` · ${a.target_message_id}`} · {a.status}
        <div className="mt-2 space-y-2">{Object.entries(a.dimensions).map(([name,d])=><div key={name}><span className="font-semibold">{name}</span> · {d.score != null ? `${d.score} (score)` : d.state}<p className="text-xs" style={muted}>{d.reason}</p></div>)}</div>
        {a.findings.map((f,i)=><div key={i} className="mt-3 text-sm"><span style={{color:"#f2b84b"}}>{f.issue || f.dimension}</span> · {f.reason}{f.quote && <blockquote className="border-l pl-3 mt-1" style={muted}>{f.message_id}: “{f.quote}”</blockquote>}</div>)}{a.error && <p style={{color:"#f2b84b"}}>{a.error}</p>}
      </div>)}
      <div className="space-y-3 max-h-96 overflow-auto">{detail.session.messages.map(m=><div key={m.id} className="border p-3" style={panel}><div className="text-xs mb-1" style={muted}>{m.id} · {m.role} · {m.status}</div><p dir="auto" className="text-sm whitespace-pre-wrap">{m.content}</p></div>)}</div>
      {detail.assessments.some(a=>a.session_revision!==detail.session.revision) && <details className="text-sm"><summary>Historical judgments from earlier transcript revisions</summary><p style={muted}>Historical results are retained for audit and excluded from current quality.</p>{detail.assessments.filter(a=>a.session_revision!==detail.session.revision).map(a=><p key={a.id} className="break-all text-xs mt-2">{a.rubric.name} v{a.rubric.version} · {a.evaluated_at} · revision {a.session_revision}</p>)}</details>}
    </div>}
    {error && <p role="alert" style={{color:"#ff6b6b"}}>{error}</p>}
  </section>;
}
