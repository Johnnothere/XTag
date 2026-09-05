import sys, threading, time
sys.path.insert(0,".")
import app, coordination
P=F=0
def chk(n,c,e=""):
    global P,F
    if c:P+=1;print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else:F+=1;print(f"  FAIL  {n}  {e}")

print("\n=== views are not engagement ===")
eb = app._engagement_breakdown("♥ 12 ▶ 2,400,000 ↺ 3")
chk("views bucketed separately", eb["views"]==2400000, str(eb))
chk("views excluded from reactions", eb["reactions"]==12, str(eb))
chk("engagement stays act-based", eb["reactions"]+eb["comments"]+eb["shares"]==15, str(eb))
agg = app._build_aggregates({"youtube":{"platform":"youtube","results":[
    {"meta":"▶ 5,000,000 ♥ 3"},{"meta":"♥ 7 \U0001f4ac 2"}]}})
t = agg["totals"]
chk("aggregate views separate", t["views"]==5000000, str(t["views"]))
chk("aggregate engagement excludes views", t["engagement"]==12, str(t["engagement"]))

print("\n=== source errors survive the merge ===")
direct={"x":{"platform":"x","results":[],"error":"HTTP 429 rate limited"}}
cse={"x":{"platform":"x","results":[{"url":"https://a/1"},{"url":"https://a/2"}],"error":None}}
out={}
for pid in set(direct)|set(cse):
    d,c=direct.get(pid),cse.get(pid)
    if d and c:
        ex={r.get("url") for r in d.get("results",[]) if r.get("url")}
        extra=[r for r in c.get("results",[]) if r.get("url") and r["url"] not in ex]
        merged=d.get("results",[])+extra
        errs=[e for e in (d.get("error"),c.get("error")) if e]
        out[pid]={"platform":pid,"results":merged,"error":"; ".join(errs) or None,
                  "partial":bool(errs and merged)}
chk("error preserved despite fallback results", out["x"]["error"]=="HTTP 429 rate limited",
    str(out["x"]["error"]))
chk("marked partial", out["x"]["partial"] is True)
chk("results still delivered", len(out["x"]["results"])==2)

print("\n=== cross-collector deduplication ===")
def art(pid,u,txt="",ts=None,auth=None):
    return {"platform":pid,"url":u,"title":txt,"excerpt":txt,"timestamp":ts,"author":auth,
            "source_type":"news"}
o={"gnews":{"platform":"gnews","results":[art("gnews","https://r.com/a?utm_source=x","short")]},
   "gdelt":{"platform":"gdelt","results":[art("gdelt","https://www.r.com/a","a much longer excerpt here","2026-08-01T00:00:00Z","Reuters")]},
   "x":{"platform":"x","results":[art("x","https://x.com/p/1"),art("x","https://x.com/p/2")]}}
removed=app._dedupe_news_urls(o)
total=sum(len(g["results"]) for g in o.values())
chk("one article, not two", removed==1 and total==3, f"removed={removed} total={total}")
kept=[d for g in o.values() for d in g["results"] if "r.com" in (d.get("url") or "")]
chk("richer record survived", kept and len(kept[0]["excerpt"])>10, str(kept)[:80])
chk("also_found_on records the other collector",
    kept and kept[0].get("also_found_on"), str(kept[0].get("also_found_on") if kept else None))
chk("social posts untouched", len(o["x"]["results"])==2)
# a duplicate that carries data the survivor lacks must not take it with it
o2={"gnews":{"platform":"gnews","results":[
        dict(art("gnews","https://r.com/b","tiny"), meta="\u25b6 3,000,000", engagement=0, views=3000000)]},
    "gdelt":{"platform":"gdelt","results":[
        art("gdelt","https://www.r.com/b","a considerably longer article body","2026-08-01T00:00:00Z","Reuters")]}}
app._dedupe_news_urls(o2)
surv=[d for g in o2.values() for d in g["results"]]
chk("dedupe absorbs data from the dropped copy",
    len(surv)==1 and surv[0].get("views")==3000000 and len(surv[0]["excerpt"])>20,
    f"views={surv[0].get('views') if surv else None} excerpt_len={len(surv[0]['excerpt']) if surv else 0}")
chk("...and keeps the timestamp it had", surv and surv[0].get("timestamp"))

# the important one: social co-sharing must survive, it is coordination's best trait
soc={"x":{"platform":"x","results":[
        {"platform":"x","url":"https://x.com/a1","author":"s1","title":"look","source_type":"social"},
        {"platform":"x","url":"https://x.com/a2","author":"s2","title":"look","source_type":"social"}]},
     "telegram":{"platform":"telegram","results":[
        {"platform":"telegram","url":"https://t.me/c/1","author":"s3","title":"look","source_type":"social"}]}}
before=sum(len(g["results"]) for g in soc.values())
app._dedupe_news_urls(soc)
after=sum(len(g["results"]) for g in soc.values())
chk("dedupe never touches social corpora", before==after, f"{before} -> {after}")

print("\n=== monitoring must observe, not replay ===")
import inspect
src=inspect.getsource(app.evaluate_watchlist)
chk("watchlist check bypasses cache", "use_cache=False" in src)
chk("...and says why", "OBSERVATION AT A POINT IN TIME" in src or "time series" in src)
chk("scheduled report bypasses cache",
    "use_cache=False" in inspect.getsource(app._run_one_subscription))

print("\n=== _claude_last_error is race-safe ===")
chk("lock exists", isinstance(app._claude_err_lock, type(threading.Lock())))
chk("accessor returns a copy", app._get_claude_error() is not app._claude_last_error)
stop=[False]; bad=[0]
def writer(i):
    while not stop[0]:
        app._set_claude_error("t", i, f"msg{i}")
def reader():
    while not stop[0]:
        e=app._get_claude_error()
        if e["status"] is not None and f"msg{e['status']}" != e["message"]:
            bad[0]+=1
ts=[threading.Thread(target=writer,args=(i,),daemon=True) for i in (1,2,3)]
ts.append(threading.Thread(target=reader,daemon=True))
for t_ in ts: t_.start()
time.sleep(0.4); stop[0]=True
for t_ in ts: t_.join(timeout=1)
chk("status and message never mismatch under concurrency", bad[0]==0, f"{bad[0]} torn reads")
app._set_claude_error()

print("\n=== velocity and propagation read the full corpus ===")
vsrc=inspect.getsource(app.compute_velocity); psrc=inspect.getsource(app.trace_propagation)
chk("velocity uses _corpus", "_corpus(platforms)" in vsrc and "= _top_docs(" not in vsrc)
# the docstring names _top_docs when explaining the bug, so match the CALL
chk("propagation uses _corpus", "_corpus(platforms)" in psrc and "= _top_docs(" not in psrc)
plats={"x":{"platform":"x","results":[
        {"platform":"x","url":"u1","timestamp":"2026-08-01T00:00:00Z"},
        {"platform":"x","url":"u2","timestamp":None}]},
       "gnews":{"platform":"gnews","results":[
        {"platform":"gnews","url":"u3","timestamp":"2026-08-02T00:00:00Z"}]}}
pr=app.trace_propagation(plats)
chk("undated counted", pr["undated"]==1, str(pr["undated"]))
chk("sample size reported", pr["sampled_from"]==3, str(pr["sampled_from"]))
chk("origin caveat present", "not the origin of the" in (pr.get("origin_caveat") or ""))
chk("per-platform dated/undated on the chain",
    all("dated_docs" in c for c in pr["propagation_chain"]))
ve=app.compute_velocity(plats)
chk("velocity reports undated", ve["undated_docs"]==1, str(ve["undated_docs"]))
chk("velocity says what it computed from", ve["rate_computed_from"]==2, str(ve["rate_computed_from"]))
empty=app.trace_propagation({"x":{"platform":"x","results":[{"platform":"x","url":"u","timestamp":None}]}})
chk("no-timestamp corpus explains itself", empty["origin"] is None and empty.get("origin_caveat"))

print("\n=== reach split ===")
docs=[{"platform":"x","url":f"u{i}","author":f"s{i%4}","engagement":1} for i in range(20)]
docs+=[{"platform":"x","url":f"o{i}","author":f"r{i}","engagement":900} for i in range(10)]
rs=coordination.reach_split(docs,[{"actors":[f"x:s{i}" for i in range(4)]}])
chk("share computed", 0 < rs["manufactured_share"] < 0.10, str(rs["manufactured_share"]))
chk("verdict is captured", "captured" in rs["verdict"])
nz=coordination.reach_split([{"platform":"x","url":"u","author":"a"}],[{"actors":["x:a"]}])
chk("no engagement data is not 0%", nz["manufactured_share"] is None, str(nz))
chk("...and says why", "cannot be split" in nz["verdict"])

print("\n=== report.html exists and renders ===")
import os
chk("template file exists", os.path.exists("templates/report.html"))
from jinja2 import Environment, FileSystemLoader
t=Environment(loader=FileSystemLoader("templates")).get_template("report.html")
empty_d={'query':'q','generated_at':'now','classification':'OSINT','totals':{},
         'confidence':{},'threat':{},'risk':{},'coordination':{},'propagation':{},
         'velocity':{},'narratives':[],'entities':[],'evidence':[],'source_mix':[],
         'engine_errors':{},'gdelt_degraded':[],'executive_brief':None}
html=t.render(d=empty_d)
chk("renders on a fully empty payload", len(html)>500)
chk("no jinja errors leaked", "{{" not in html and "{%" not in html)
unb=dict(empty_d); unb["coordination"]={"coordination_score":47,"risk":"unbanded",
                                        "caveat":"No matched organic baseline."}
h2=t.render(d=unb)
chk("unbanded coordination shows the score", "47" in h2)
chk("unbanded shows NO risk band", ">unbanded<" not in h2 and "Not banded" in h2)
chk("unbanded prints the caveat", "No matched organic baseline." in h2)
band=dict(empty_d); band["coordination"]={"coordination_score":80,"risk":"high","baseline_ratio":9.1}
chk("a real band IS shown", "high" in t.render(d=band))

print("\n=== watchlist alerts from narrative tracking ===")
import narratives as N
rules = dict(app.DEFAULT_RULES)
def fire(events, rules=rules):
    """Run just the tracking-alert block against a synthetic payload."""
    payload = {"narrative_tracking": {"events": events}, "coordination": {},
               "velocity": {}, "totals": {}, "narratives": []}
    alerts = []
    nt = payload.get("narrative_tracking") or {}
    for ev in (nt.get("events") or []):
        if not isinstance(ev, dict): continue
        kind = ev.get("kind"); label = ev.get("label") or "(unnamed narrative)"
        if kind == "growth" and rules.get("narrative_growth_pct") is not None:
            thr = float(rules["narrative_growth_pct"]) / 100.0
            measured = ev.get("share_delta"); basis = "share of corpus"
            if measured is None:
                measured = ev.get("delta"); basis = "raw document count, no corpus size recorded"
            if measured is not None and measured >= thr:
                alerts.append({"type":"narrative_growth",
                    "severity":"high" if measured >= thr*2 else "medium",
                    "message":f"'{label}' grew {measured:+.0%} since the last check ({basis})"})
        elif kind == "drift" and rules.get("narrative_drift"):
            alerts.append({"type":"narrative_drift","severity":"high","message":"drift"})
        elif kind in ("merge","split") and rules.get("narrative_merge_split"):
            alerts.append({"type":f"narrative_{kind}","severity":"medium","message":kind})
        elif kind == "death" and rules.get("narrative_death"):
            alerts.append({"type":"narrative_death","severity":"low","message":"death"})
    return alerts

chk("growth above threshold fires",
    [a["type"] for a in fire([{"kind":"growth","label":"X","share_delta":0.60,"delta":0.9}])]
    == ["narrative_growth"])
chk("growth below threshold does not",
    fire([{"kind":"growth","label":"X","share_delta":0.10,"delta":0.9}]) == [],
    "raw delta 0.9 must not fire when share only moved 0.10")
chk("SHARE wins over raw count",
    fire([{"kind":"growth","label":"X","share_delta":0.05,"delta":5.0}]) == [],
    "a 500% raw rise in a corpus that grew as much is not an alert")
chk("falls back to raw when no denominator, and says so",
    "no corpus size recorded" in fire([{"kind":"growth","label":"X",
        "share_delta":None,"delta":0.9}])[0]["message"])
chk("held_share never alerts",
    fire([{"kind":"held_share","label":"X","share_delta":0.02,"delta":1.0}]) == [],
    "collection volume must not page anyone")
chk("drift alerts at high severity",
    fire([{"kind":"drift","label":"X","from_label":"a","to_label":"b"}])[0]["severity"]=="high")
chk("merge and split alert", len(fire([{"kind":"merge","label":"X"},{"kind":"split","label":"Y"}]))==2)
chk("death is off by default", fire([{"kind":"death","label":"X","misses":3}]) == [])
chk("death fires when enabled",
    len(fire([{"kind":"death","label":"X","misses":3}], {**rules,"narrative_death":True}))==1)
chk("malformed events are skipped", fire([None,"x",{},{"kind":"growth"}]) == [])

chk("tracking rules are in DEFAULT_RULES",
    all(k in app.DEFAULT_RULES for k in
        ("narrative_growth_pct","narrative_drift","narrative_merge_split","narrative_death")),
    str(sorted(app.DEFAULT_RULES)))
chk("death defaults off", app.DEFAULT_RULES["narrative_death"] is False)

print("\n=== corpus denominator reaches track() ===")
import inspect
src = inspect.getsource(app._run_full_search)
chk("corpus_size passed", "corpus_size=len(claims)" in src)
chk("clustering method recorded on the payload", 'tracking["clustering"]' in src)
chk("semantic availability reported", 'tracking["semantic"]' in src)

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
