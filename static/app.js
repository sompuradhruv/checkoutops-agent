"use strict";
// CheckoutOps Agent UI. No framework, no build step. All model output is rendered with
// textContent, never innerHTML: the report is untrusted text and could contain markup.

const state={config: null, scenarios: [], current: null, runId: null, source: null, steps: new Map(), usage: null, finished: null, report: null};
const $=(id)=>document.getElementById(id);

function el(tag, attrs, ...children){
  const node=document.createElement(tag);
  for(const [k, v] of Object.entries(attrs||{})){
    if(v===undefined||v===null||v===false) continue;
    if(k==="class") node.className=v;
    else if(k==="text") node.textContent=v;
    else if(k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v===true?"":v);
  }
  for(const c of children.flat()){
    if(c===null||c===undefined||c===false) continue;
    node.append(c instanceof Node?c:document.createTextNode(String(c)));
  }
  return node;
}

function svg(tag, attrs){
  const node=document.createElementNS("http://www.w3.org/2000/svg", tag);
  for(const [k, v] of Object.entries(attrs||{})){
    if(k==="text") node.textContent=v; else node.setAttribute(k, v);
  }
  return node;
}

async function api(path, options){
  const res=await fetch(path, options);
  let body=null;
  try{ body=await res.json(); }catch(e){ body=null; }
  if(!res.ok){
    const msg=body&&body.detail?(typeof body.detail==="string"?body.detail:JSON.stringify(body.detail)):`Request failed (${res.status})`;
    throw new Error(msg);
  }
  return body;
}

const CATEGORY={bad_deploy: "Bad deploy", dependency_outage: "Dependency outage", idempotency_bug: "Idempotency bug",
  expired_credential: "Expired credential", traffic_spike_no_bug: "Expected traffic, no bug", unknown: "Unknown"};

function clock(iso){ return iso?iso.slice(11, 16)+" UTC":""; }
function fmtNum(v){
  if(typeof v!=="number") return String(v);
  if(Math.abs(v)>=1000) return Math.round(v).toLocaleString("en-US");
  if(Math.abs(v)>=100) return String(Math.round(v));
  return String(+v.toFixed(Math.abs(v)<1?3:2));
}
function fmtMetric(v, unit){
  if(unit==="ratio") return (v*100).toFixed(v<0.1?1:0)+"%";
  if(unit==="ms") return fmtNum(v)+" ms";
  return fmtNum(v)+(unit?" "+unit:"");
}
function money(v){ return "$"+(v||0).toFixed(v<0.01?4:3); }

// ---------------------------------------------------------------- tabs
function showTab(){
  const name=(location.hash||"#console").slice(1);
  const panels={console: "panel-console", evals: "panel-evals", how: "panel-how"};
  const active=panels[name]?name:"console";
  for(const [key, id] of Object.entries(panels)){
    $(id).hidden=key!==active;
    const tab=$("tab-"+key);
    if(key===active) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
  }
  if(active==="evals") loadEvals();
}

// ---------------------------------------------------------------- runtime status
function renderRuntime(){
  const c=state.config;
  const node=$("runtime");
  node.replaceChildren();
  let cls="", text;
  if(c.provider==="Scripted (no LLM)"){ text="Scripted dev mode: no language model, canned steps for UI testing"; }
  else if(c.liveAvailable){ cls="live"; text=`Live runs on ${c.provider}, model ${c.model}`+(c.escalationModel?`, escalates to ${c.escalationModel}`:""); }
  else if(c.replays.length){ cls="replay"; text="Live runs are paused. Recordings of real runs are available."; }
  else { text="No model connected and no recordings yet."; }
  node.className="runtime "+cls;
  node.append(el("span", {class: "dot", "aria-hidden": "true"}), text);
}

// ---------------------------------------------------------------- alert queue
function renderQueue(){
  const list=$("queue");
  list.replaceChildren();
  for(const s of state.scenarios){
    const btn=el("button", {class: "queue-item", type: "button", "aria-current": state.current&&state.current.id===s.id?"true":"false",
      onclick: ()=>selectScenario(s.id)},
      el("span", {class: "q-time", text: "Fired "+clock(s.alert.firedAt)}),
      el("span", {class: "q-title", text: s.title}),
      el("span", {class: "q-alert", text: s.alert.title}),
      s.hasReplay?el("span", {class: "q-tag", text: "Recording available"}):null);
    list.append(el("li", {}, btn));
  }
}

async function selectScenario(id){
  if(state.source){ state.source.close(); state.source=null; }
  const data=await api(`/api/scenarios/${encodeURIComponent(id)}/overview`);
  state.current=data;
  history.replaceState(null, "", `?alert=${encodeURIComponent(id)}${location.hash}`);
  renderQueue();
  const a=data.alert;
  $("fired").textContent=`Paged at ${clock(a.firedAt)} on ${a.service}`;
  $("alert-title").textContent=a.title;
  $("teaser").textContent=data.teaser;
  drawChart(data);
  resetRun();
}

function drawChart(data){
  const chart=$("chart");
  chart.replaceChildren();
  const a=data.alert;
  const values=data.series.map(p=>p.v);
  const W=720, H=190, left=8, right=10, top=22, bottom=28;
  const lo=Math.min(0, ...values);
  let hi=Math.max(a.threshold*1.15, ...values)*1.05;
  if(a.unit==="ratio") hi=Math.min(hi, 1);
  const x=(i)=>left+i*(W-left-right)/(values.length-1);
  const y=(v)=>top+(H-top-bottom)*(1-(v-lo)/(hi-lo||1));
  for(const f of [0.5, 1]){
    const v=lo+(hi-lo)*f;
    chart.append(svg("line", {class: "grid", x1: left, x2: W-right, y1: y(v), y2: y(v)}));
    chart.append(svg("text", {x: left, y: y(v)-5, text: fmtMetric(v, a.unit)}));
  }
  chart.append(svg("line", {class: "grid", x1: left, x2: W-right, y1: y(lo), y2: y(lo)}));
  const pts=values.map((v, i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  if(a.comparison==="above") chart.append(svg("path", {class: "area", d: `M${x(a.firedIndex)},${y(lo)} L${pts.slice(a.firedIndex).join(" L")} L${x(values.length-1)},${y(lo)} Z`}));
  chart.append(svg("polyline", {class: "line", points: pts.join(" ")}));
  chart.append(svg("line", {class: "threshold", x1: left, x2: W-right, y1: y(a.threshold), y2: y(a.threshold)}));
  chart.append(svg("text", {class: "th-label", x: x(Math.max(2, a.firedIndex-4)), y: a.comparison==="above"?y(a.threshold)-6:y(a.threshold)+16, "text-anchor": "end", text: `Alert ${a.comparison} ${fmtMetric(a.threshold, a.unit)}`}));
  chart.append(svg("line", {class: "fired-line", x1: x(a.firedIndex), x2: x(a.firedIndex), y1: top, y2: H-bottom}));
  chart.append(svg("text", {x: left, y: H-8, text: clock(data.series[0].t)}));
  chart.append(svg("text", {x: x(a.firedIndex), y: H-8, "text-anchor": "middle", text: "paged "+clock(a.firedAt)}));
  chart.append(svg("text", {class: "now-label", x: W-right, y: H-8, "text-anchor": "end", text: "now"}));
  $("chart-caption").textContent=`${a.metric} on ${a.service}, last 2 hours, one point per minute.`;
}

// ---------------------------------------------------------------- running
function resetRun(){
  state.steps=new Map(); state.usage=null; state.finished=null; state.report=null; state.runId=null;
  $("timeline").replaceChildren();
  $("receipt-slot").replaceChildren();
  $("truth").hidden=true; $("truth").replaceChildren();
  const s=state.scenarios.find(x=>x.id===state.current.id);
  const c=state.config;
  const canLive=c.liveAvailable;
  const btn=$("run-btn");
  const note=$("run-note");
  note.className="run-note";
  if(!canLive&&!s.hasReplay){
    btn.disabled=true;
    note.textContent="This server has no model connected and no recording for this alert yet.";
  }else{
    btn.disabled=false;
    note.textContent=canLive?`Live runs are limited to ${c.limits.liveRunsPerHour} per hour per visitor.`:"Live runs are paused, so this plays a recording of a real run.";
  }
  $("mode").querySelector('option[value="replay"]').disabled=!s.hasReplay;
  if(!s.hasReplay) $("mode").value="auto";
}

async function startRun(){
  const btn=$("run-btn");
  btn.disabled=true;
  resetRun();
  btn.disabled=true;
  const note=$("run-note");
  note.textContent="Starting…";
  let res;
  try{
    res=await api("/api/runs", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({scenarioId: state.current.id, mode: $("mode").value})});
  }catch(e){
    note.className="run-note warn"; note.textContent=e.message; btn.disabled=false; return;
  }
  state.runId=res.runId;
  if(res.mode==="replay"&&res.reason){ note.className="run-note warn"; note.textContent=res.reason; }
  else note.textContent=res.mode==="live"?"Investigating live. Steps appear as the model takes them.":"Playing a recording.";
  addWaiting();
  const source=new EventSource(`/api/runs/${res.runId}/events`);
  state.source=source;
  source.onmessage=(m)=>handleEvent(JSON.parse(m.data));
  source.addEventListener("end", ()=>{ source.close(); state.source=null; removeWaiting(); btn.disabled=false; btn.textContent="Investigate again"; });
  source.onerror=()=>{ if(source.readyState===EventSource.CLOSED){ removeWaiting(); btn.disabled=false; } };
}

function addWaiting(){
  removeWaiting();
  $("timeline").append(el("li", {class: "t-wait", id: "waiting"}, "Waiting for the model…"));
}
function removeWaiting(){ const w=$("waiting"); if(w) w.remove(); }
function push(node){ removeWaiting(); $("timeline").append(node); if(!state.finished) addWaiting(); }

function handleEvent(ev){
  switch(ev.type){
    case "run_started": {
      let text;
      if(ev.mode==="live") text=`Live run on ${ev.provider}`;
      else text=`Recording of a run from ${ev.recordedAt?ev.recordedAt.replace("T", " ").replace("Z", " UTC"):"earlier"} with ${ev.recordedModel||"the agent"}`+(ev.scripted?" (scripted test data, not a model)":"");
      push(el("li", {class: "t-start"}, el("strong", {text}), el("div", {class: "meta", text: "Alert sent to the agent with the read-only tools it may call."})));
      break;
    }
    case "agent_started":
      push(el("li", {class: "t-start"}, el("span", {class: "meta", text: `Model ${ev.model}, prompt version ${ev.promptVersion}`})));
      break;
    case "thinking":
      push(el("li", {class: "t-think"}, ev.text));
      break;
    case "tool_call": {
      const n=state.steps.size+1;
      const args=Object.entries(ev.args||{}).map(([k, v])=>[el("span", {class: "arg", text: `${k}: `}), JSON.stringify(v)]);
      const call=el("div", {class: "call"}, el("span", {class: "fn", text: ev.name}), "(", ...args.flatMap((a, i)=>i?[", ", ...a]:a), ")");
      const li=el("li", {class: "t-tool"}, el("span", {class: "step-no", text: String(n)}), call, el("div", {class: "result", text: "Running…"}));
      state.steps.set(ev.id, li);
      push(li);
      break;
    }
    case "tool_result": {
      const li=state.steps.get(ev.id);
      if(!li) break;
      li.classList.add("done");
      const result=li.querySelector(".result");
      result.textContent=summarise(ev.name, ev.output);
      if(!ev.ok) result.classList.add("failed");
      const badges=el("span", {class: "badges"},
        ev.redactions?el("span", {class: "badge redact", text: `${ev.redactions} secret${ev.redactions>1?"s":""} redacted`}):null,
        ev.injection?el("span", {class: "badge inject", text: "injection flagged"}):null);
      if(ev.redactions||ev.injection) li.querySelector(".call").append(badges);
      li.append(el("details", {class: "raw"}, el("summary", {text: `What the model saw (${ev.chars.toLocaleString("en-US")} characters)`}), el("pre", {text: pretty(ev.output)})));
      break;
    }
    case "guardrail": {
      const kinds={redaction: "Secret redacted", injection: "Prompt injection flagged", validation: "Report rejected", limit: "Limit reached"};
      push(el("li", {class: "t-guard"}, el("span", {class: "g-kind", text: kinds[ev.kind]||"Guardrail"}), el("div", {class: "g-text", text: ev.detail})));
      break;
    }
    case "escalation":
      push(el("li", {class: "t-escalate"}, el("strong", {text: `Escalating from ${ev.fromModel} to ${ev.toModel}`}), el("div", {class: "meta", text: ev.reason})));
      break;
    case "usage": state.usage=ev; break;
    case "report": state.report=ev.report; break;
    case "run_finished":
      state.finished=ev;
      removeWaiting();
      if(state.report) renderReceipt(state.report, ev);
      break;
    case "score": state.finished=state.finished||{}; removeWaiting(); if(state.report&&!$("receipt-slot").firstChild) renderReceipt(state.report, state.finished); renderTruth(ev); break;
    case "error": push(el("li", {class: "t-error", text: ev.message})); break;
  }
}

function parseOutput(text){
  try{
    let o=JSON.parse(text);
    if(o&&o.untrustedToolOutput) o=o.untrustedToolOutput;
    return o;
  }catch(e){ return null; }
}
function pretty(text){ const o=parseOutput(text); return o?JSON.stringify(o, null, 2):text; }

function summarise(name, text){
  const o=parseOutput(text);
  if(!o) return "Returned a long result (truncated for the model).";
  if(o.error) return "Error: "+o.error;
  if(o.note&&Object.keys(o).length===1) return o.note;
  try{
    switch(name){
      case "getServiceHealth": {
        const bad=o.services.filter(s=>s.status!=="healthy").map(s=>s.service);
        return bad.length?`Degraded: ${bad.join(", ")}. Others healthy.`:"All six services within normal ranges.";
      }
      case "queryMetrics": {
        const ch=o.changeDetected;
        const pct=typeof o.changePct==="number"?` (${o.changePct>0?"+":""}${fmtNum(o.changePct)}%)`:"";
        return `${o.metric}: baseline ${fmtNum(o.baseline)}, now ${fmtNum(o.latest)}${pct}. `+(ch?`Changed ${ch.minutesAgo} min ago, at ${clock(ch.startedAt)}.`:"No change detected.");
      }
      case "searchLogs":
        if(!o.totalMatches) return "No matching log lines.";
        return `${o.totalMatches.toLocaleString("en-US")} matching lines. Most common: "${o.topPatterns[0].pattern.slice(0, 110)}"`;
      case "getTraces":
        return o.traces.length?`${o.matched} traces match. Slowest ${fmtNum(o.traces[0].durationMs)} ms with ${o.traces[0].spans.length} spans.`:"No matching traces.";
      case "listDeploys": {
        const d=o.deploys;
        return d.length?`${d.length} deploys in the ${o.window}. Latest: ${d[0].deployId} ${d[0].service} ${d[0].version}, ${d[0].minutesAgo} min ago.`:"No deploys in that window.";
      }
      case "getDeployDiff": return `${o.deployId} (${o.service} ${o.version}): ${o.summary}`;
      case "getServiceConfig": return `Configuration of ${o.service}.`;
      case "searchRunbooks": {
        if(!o.results.length) return "No runbook matched.";
        const top=o.results[0];
        return `Top match: ${top.title.replace(/^Runbook:\s*/, "")}, section "${top.section}"`+(o.results.length>1?` (+${o.results.length-1} more)`:"");
      }
    }
  }catch(e){ /* fall through */ }
  return "Done.";
}

// ---------------------------------------------------------------- receipt
function row(k, v, grand){
  return el("div", {class: "r-row"+(grand?" grand":"")}, el("span", {class: "k", text: k}), el("span", {class: "leader", "aria-hidden": "true"}), el("span", {class: "v", text: v}));
}

function renderReceipt(r, fin){
  const slot=$("receipt-slot");
  slot.replaceChildren();
  const labels=state.config.actionLabels||{};
  const actions=r.recommendedActions.map((a, i)=>{
    const btn=el("button", {class: "approve", type: "button", onclick: ()=>approve(i, btn, wrap)}, "Approve");
    const wrap=el("div", {class: "r-action"},
      el("div", {}, el("span", {class: "a-name", text: labels[a.action]||a.action}), " ", el("span", {text: a.target})),
      btn,
      el("div", {class: "a-meta"}, el("span", {class: "risk-"+a.risk, text: `${a.risk} risk. `}), a.rationale));
    return wrap;
  });
  const receipt=el("div", {class: "receipt"},
    el("div", {class: "r-head"},
      el("div", {class: "r-brand", text: "CheckoutOps incident report"}),
      el("div", {class: "r-sub", text: `Run ${state.runId||""}, paged ${clock(state.current.alert.firedAt)}`}),
      el("div", {class: "r-sev "+r.severity, text: r.severity==="none"?"No incident":r.severity})),
    el("h3", {text: r.title}),
    el("p", {class: "r-summary", text: r.summary}),
    el("div", {class: "r-block"}, el("div", {class: "r-label", text: "Customer impact"}), el("div", {text: r.customerImpact})),
    el("div", {class: "r-block r-cause"}, el("div", {class: "r-label", text: "Root cause"}),
      el("div", {}, el("span", {class: "cat", text: CATEGORY[r.rootCause.category]||r.rootCause.category}), `: ${r.rootCause.culprit}`),
      el("div", {text: r.rootCause.description})),
    el("div", {class: "r-block"}, el("div", {class: "r-label", text: "Evidence"}),
      el("ul", {}, r.evidence.map(e=>el("li", {text: `${e.source}: ${e.detail}`})))),
    el("div", {class: "r-block"}, el("div", {class: "r-label", text: "Recommended actions, each needs your approval"}), ...actions),
    r.followUps&&r.followUps.length?el("div", {class: "r-block"}, el("div", {class: "r-label", text: "Follow-ups"}), el("ul", {}, r.followUps.map(f=>el("li", {text: f})))):null,
    r.suspiciousContent&&r.suspiciousContent.length?el("div", {class: "r-block r-suspicious"}, el("div", {class: "r-label", text: "Ignored instructions found in telemetry"}),
      el("ul", {}, r.suspiciousContent.map(f=>el("li", {text: f})))):null,
    el("div", {class: "r-total"},
      row("Confidence", Math.round(r.confidence*100)+"%"),
      fin&&fin.steps!==undefined?row("Model calls", String(fin.steps)):null,
      fin&&fin.inputTokens!==undefined?row("Tokens in / out", `${fin.inputTokens.toLocaleString("en-US")} / ${fin.outputTokens.toLocaleString("en-US")}`):null,
      fin&&fin.latencyMs!==undefined?row("Time", (fin.latencyMs/1000).toFixed(1)+" s"):null,
      row("Cost of this investigation", fin&&fin.costUsd!==undefined?money(fin.costUsd):"n/a", true)),
    el("div", {class: "r-foot", text: "Nothing has changed yet. Approved actions run against the simulator."}));
  const shell=el("div", {class: "receipt-shell printing"}, receipt);
  slot.append(shell);
}

async function approve(i, btn, wrap){
  btn.disabled=true;
  btn.textContent="Applying…";
  try{
    const res=await api(`/api/runs/${state.runId}/actions/${i}/approve`, {method: "POST"});
    btn.textContent="Approved";
    wrap.append(el("div", {class: "outcome "+(res.resolved?"good":"bad"), role: "status", text: res.outcome}));
  }catch(e){
    btn.disabled=false; btn.textContent="Approve";
    wrap.append(el("div", {class: "outcome bad", role: "status", text: e.message}));
  }
}

function renderTruth(ev){
  const box=$("truth");
  const passed=ev.score.passed;
  box.className="truth "+(passed?"pass":"fail");
  box.replaceChildren(
    el("h3", {id: "truth-heading"}, "What was actually injected: ", el("span", {class: "verdict "+(passed?"pass":"fail"), text: passed?"agent got it right":"agent missed"})),
    el("p", {text: ev.groundTruth}),
    el("ul", {class: "checks"}, ev.score.checks.map(c=>el("li", {class: c.passed?"ok":"no"}, el("span", {class: "cname", text: c.name}), ": ", c.detail))),
    el("p", {}, el("strong", {text: "Concept: "}), ev.concept));
  box.hidden=false;
}

// ---------------------------------------------------------------- evals
let evalsLoaded=false;
async function loadEvals(){
  if(evalsLoaded) return;
  evalsLoaded=true;
  const body=$("evals-body");
  let data;
  try{ data=await api("/api/evals/latest"); }catch(e){ body.replaceChildren(el("p", {text: e.message})); return; }
  if(!data.available){
    body.replaceChildren(el("div", {class: "empty"},
      el("p", {}, "No eval results on this server yet. Run ", el("code", {text: "python -m evals.run_evals --models gpt-5-mini,gpt-5 --repeats 3"}), " and redeploy to publish them here.")));
    return;
  }
  const scen=Object.fromEntries(state.scenarios.map(s=>[s.id, s.title]));
  const models=Object.keys(data.summary);
  const cls=(r)=>r>=0.99?"cell-pass":r>=0.5?"cell-mid":"cell-fail";
  const pct=(r)=>Math.round(r*100)+"%";
  const summary=el("table", {},
    el("thead", {}, el("tr", {}, el("th", {scope: "col", text: "Model"}), el("th", {class: "num", scope: "col", text: "Pass rate"}),
      el("th", {class: "num", scope: "col", text: "Avg cost per run"}), el("th", {class: "num", scope: "col", text: "Avg time"}), el("th", {class: "num", scope: "col", text: "Avg model calls"}))),
    el("tbody", {}, models.map(m=>{ const s=data.summary[m]; return el("tr", {},
      el("th", {scope: "row", text: m}), el("td", {class: "num "+cls(s.passRate), text: pct(s.passRate)}),
      el("td", {class: "num", text: money(s.avgCostUsd)}), el("td", {class: "num", text: s.avgLatencyS.toFixed(1)+" s"}), el("td", {class: "num", text: String(s.avgSteps)})); })));
  const perScenario=el("table", {},
    el("thead", {}, el("tr", {}, el("th", {scope: "col", text: "Scenario"}), ...models.map(m=>el("th", {class: "num", scope: "col", text: m})))),
    el("tbody", {}, data.scenarios.map(sid=>el("tr", {}, el("th", {scope: "row", text: scen[sid]||sid}),
      ...models.map(m=>{ const r=data.summary[m].perScenario[sid]; return el("td", {class: "num "+cls(r), text: pct(r)}); })))));
  const failures=data.runs.filter(r=>!r.passed);
  const failList=failures.length?el("ul", {class: "plain"}, failures.slice(0, 12).map(r=>el("li", {},
    el("strong", {text: `${r.model}, ${scen[r.scenarioId]||r.scenarioId}: `}), r.checks.filter(c=>!c.passed).map(c=>`${c.name} (${c.detail})`).join("; ")||r.error||"failed"))):el("p", {text: "No failures in this run."});
  body.replaceChildren(summary,
    el("p", {class: "meta-line", text: `${data.repeats} runs per scenario per model. Generated ${data.generatedAt.replace("T", " ")} with prompt version ${data.promptVersion}. Costs use list prices and are estimates.`}),
    el("h3", {text: "Pass rate by scenario"}), perScenario,
    el("h3", {text: "What failed"}), failList);
}

// ---------------------------------------------------------------- boot
async function boot(){
  window.addEventListener("hashchange", showTab);
  $("run-btn").addEventListener("click", startRun);
  try{
    const [config, scenarios]=await Promise.all([api("/api/config"), api("/api/scenarios")]);
    state.config=config; state.scenarios=scenarios;
  }catch(e){
    $("runtime").textContent="Could not reach the server: "+e.message;
    return;
  }
  renderRuntime();
  const tbody=$("concepts");
  const concepts={"latency-bad-deploy": "Safe deployment: roll back first", "payment-provider-outage": "Circuit breakers, retries with backoff, failover",
    "double-charge": "Idempotency keys", "expired-credential": "Secrets management, managed identity", "prompt-injection": "Prompt injection defence",
    "traffic-spike-sale": "Not every alert is an incident"};
  for(const s of state.scenarios) tbody.append(el("tr", {}, el("td", {text: s.title}), el("td", {text: concepts[s.id]||""})));
  const wanted=new URLSearchParams(location.search).get("alert");
  const first=state.scenarios.find(s=>s.id===wanted)||state.scenarios[0];
  await selectScenario(first.id);
  showTab();
}

boot();
