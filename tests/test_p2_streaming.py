import time, sys, json, re
import app
P=F=0
def chk(n,c,e=""):
    global P,F
    if c: P+=1; print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else: F+=1; print(f"  FAIL  {n}  {e}")

def doc(pid,i,t): return {"platform":pid,"title":t,"excerpt":t,
    "url":f"https://{pid}.ex/{i}","timestamp":"2026-08-01T00:00:00Z","meta":{"likes":i}}
def mk(pid,n,txt,delay=0.2):
    def f(q):
        time.sleep(delay)
        return {"platform":pid,"results":[doc(pid,i,txt) for i in range(n)],"error":None}
    return f

app.API_PLATFORMS={"x":mk("x",20,"#covid1948 protest Tehran"),
                   "youtube":mk("youtube",20,"covid1948 explained")}
app.search_serpapi=lambda q:{}
app.gdelt.snapshot=lambda q:{"volume":{"n":5}}
app.BABELSTREET_API_KEY=""
app.SEARCH_POOL_TIMEOUT=5
app.SSE_HEARTBEAT=0.2

# make INTERPRETATION slow, collection fast — the whole premise of P2
SLOW=1.5
app.extract_narratives_v2 = lambda o,q,**k: (time.sleep(SLOW), [{"label":"n"}])[1]
app.extract_entities      = lambda o,q,**k: (time.sleep(SLOW), {"entities":[{"name":"E"}]})[1]
app.extract_real_world_events = lambda o,q,**k: (time.sleep(SLOW), [{"label":"protest"}])[1]
app.ANTHROPIC_API_KEY=""

print("\n=== A. the callback fires before interpretation ===")
marks={}
def cb(p):
    marks["t"]=time.monotonic(); marks["p"]=p
t0=time.monotonic()
full=app._run_full_search("#covid1948", use_cache=False, on_phase1=cb)
t_end=time.monotonic()
chk("callback fired", "t" in marks)
chk("phase1 arrives well before completion",
    (marks["t"]-t0) < (t_end-t0)*0.6,
    f"phase1 at {marks['t']-t0:.2f}s, done at {t_end-t0:.2f}s")
p1=marks["p"]
chk("phase1 marked partial", p1.get("phase")==1 and p1.get("partial") is True)
chk("phase1 has the corpus", (p1.get("totals") or {}) and p1["platforms"]["x"]["results"])
chk("phase1 has relevance report", bool(p1.get("relevance")))
chk("phase1 has sentiment", "scored" in (p1.get("sentiment") or {}))
chk("phase1 has NO narratives yet", not p1.get("narratives"), str(p1.get("narratives")))
chk("phase1 has NO entities yet", not (p1.get("entities") or {}).get("entities"))
chk("phase1 has NO threat yet", not p1.get("threat"))
chk("phase2 HAS narratives", bool(full.get("narratives")))
chk("phase2 HAS entities", bool((full.get("entities") or {}).get("entities")))
chk("phase2 HAS threat", bool(full.get("threat")), str(list(full))[:60])
chk("phase1 timing recorded", "phase1" in (full.get("timings") or {}))

print("\n=== B. no callback == original behaviour ===")
r=app._run_full_search("#covid1948 b", use_cache=False)
chk("still returns a complete payload", bool(r.get("narratives")) and "totals" in r)
chk("no phase marker on the plain path", "phase" not in r)

print("\n=== C. the SSE endpoint actually streams ===")
c=app.app.test_client()
t0=time.monotonic()
resp=c.get("/api/search/stream?q=%23covid1948c")
chk("content type", resp.mimetype=="text/event-stream", resp.mimetype)
chk("no-transform cache header", "no-transform" in resp.headers.get("Cache-Control",""))
chk("nginx buffering disabled", resp.headers.get("X-Accel-Buffering")=="no")
chk("not compressed", "Content-Encoding" not in resp.headers,
    resp.headers.get("Content-Encoding"))

arrivals=[]
buf=""
for chunk in resp.response:
    buf += chunk.decode()
    arrivals.append((time.monotonic()-t0, chunk.decode()[:40]))
chk("stream opened first", arrivals and arrivals[0][1].startswith(": stream open"))

ev=re.findall(r"event: (\w+)\ndata: (.*)", buf)
names=[e[0] for e in ev]
chk("phase1 then phase2, in order", names==["phase1","phase2"], str(names))
t_p1=[t for t,c in arrivals if "phase1" in c]
t_p2=[t for t,c in arrivals if "phase2" in c]
chk("phase1 chunk arrived strictly earlier",
    t_p1 and t_p2 and t_p2[0]-t_p1[0] > SLOW*0.6,
    f"phase1 @{t_p1[0]:.2f}s, phase2 @{t_p2[0]:.2f}s, gap {t_p2[0]-t_p1[0]:.2f}s")
chk("heartbeats sent while working",
    any(c.startswith(": keepalive") for _,c in arrivals),
    f"{sum(1 for _,c in arrivals if c.startswith(': keepalive'))} frames")

d1=json.loads(ev[0][1]); d2=json.loads(ev[1][1])
chk("phase1 payload partial", d1["phase"]==1 and d1["partial"] is True)
chk("phase2 payload complete", d2["phase"]==2 and d2["partial"] is False and d2["narratives"])
chk("both events parse as JSON", isinstance(d1,dict) and isinstance(d2,dict))

print("\n=== D. errors reach the client as an event, not a dead socket ===")
_orig=app._run_full_search
app._run_full_search=lambda *a,**k:(_ for _ in ()).throw(RuntimeError("upstream exploded"))
r2=app.app.test_client().get("/api/search/stream?q=boom")
body=b"".join(r2.response).decode()
chk("error event emitted", "event: error" in body, body[:120])
chk("message carried", "upstream exploded" in body)
app._run_full_search=_orig

print("\n=== E. compression config cannot silently break the stream ===")
chk("event-stream not compressible", "text/event-stream" not in app.app.config.get("COMPRESS_MIMETYPES",[]))
chk("COMPRESS_STREAMS off", app.app.config.get("COMPRESS_STREAMS") is False)

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
