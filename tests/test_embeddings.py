import sys, math, types
sys.path.insert(0,".")
import embeddings as E
P=F=0
def chk(n,c,e=""):
    global P,F
    if c:P+=1;print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else:F+=1;print(f"  FAIL  {n}  {e}")

def reset():
    E._cache.clear(); E._anchors=None
    E.VOYAGE_API_KEY=""; E.OPENAI_API_KEY=""
    E._last_error.update({"at":None,"backend":None,"message":None})

print("\n=== degrades to nothing, loudly ===")
reset()
chk("no backend configured", E.backend() is None and not E.available())
v,n = E.embed(["a","b"]); chk("returns (None, 'unavailable')", v is None and n=="unavailable", n)
chk("empty input is not an error", E.embed([])==([], "empty"))
st=E.status(); chk("status says semantic is off", st["semantic"] is False and st["backend"]=="none")
chk("status explains how to turn it on", "VOYAGE_API_KEY" in st["note"])

print("\n=== cosine ===")
chk("identical", abs(E.cosine([1,2,3],[1,2,3])-1.0)<1e-9)
chk("orthogonal", abs(E.cosine([1,0],[0,1]))<1e-9)
chk("opposite", abs(E.cosine([1,0],[-1,0])+1.0)<1e-9)
chk("magnitude-invariant", abs(E.cosine([1,2],[2,4])-1.0)<1e-9)
for bad in (([],[]), (None,[1]), ([1,2],[1]), ([0,0],[1,1])):
    chk(f"guards {bad}", E.cosine(*bad)==0.0)

print("\n=== hosted backend, mocked transport ===")
# No key here to call the real API, so the CONTRACT is verified against a fake:
# ordering by `index`, batching, caching, and failure behaviour.
calls={"n":0,"batches":[]}
class R:
    def __init__(self,payload,code=200): self._p=payload; self.status_code=code; self.text=""
    def json(self): return self._p
def fake_post(url, headers=None, json=None, timeout=None):
    calls["n"]+=1; inp=json["input"]; calls["batches"].append(len(inp))
    # deliberately return OUT OF ORDER with explicit indexes
    data=[{"index":i,"embedding":[float(len(t)), 1.0, 0.0]} for i,t in enumerate(inp)]
    return R({"data":list(reversed(data))})
reset(); E.VOYAGE_API_KEY="test-key"; E.requests = types.SimpleNamespace(post=fake_post)
chk("backend now voyage", E.backend()=="voyage")
v,n = E.embed(["a","bb","ccc"])
chk("returns vectors", v is not None and len(v)==3, n)
chk("respects `index`, not response order",
    v and v[0][0]==1.0 and v[1][0]==2.0 and v[2][0]==3.0, str([x[0] for x in v] if v else None))
before=calls["n"]; E.embed(["a","bb","ccc"])
chk("second call is fully cached", calls["n"]==before, f"{calls['n']-before} extra calls")
E.embed(["a","bb","ccc","dddd"])
chk("only the new text is fetched", calls["batches"][-1]==1, str(calls["batches"]))

E.EMBED_BATCH=2; reset(); E.VOYAGE_API_KEY="k"; calls["batches"]=[]
E.embed(["a","b","c","d","e"])
chk("batches at EMBED_BATCH", calls["batches"]==[2,2,1], str(calls["batches"]))
E.EMBED_BATCH=96

print("\n=== failure is total, never partial ===")
reset(); E.VOYAGE_API_KEY="k"
E.requests = types.SimpleNamespace(post=lambda *a, **k: R({"error":"nope"}, 500))
v,n = E.embed(["a"]); chk("HTTP error -> None", v is None and n=="voyage:failed", n)
chk("error recorded", "500" in (E.last_error().get("message") or ""), str(E.last_error()))
reset(); E.VOYAGE_API_KEY="k"
E.requests = types.SimpleNamespace(post=lambda *a, **k: R({"data":[{"index":0,"embedding":[1.0]}]}))
v,n = E.embed(["a","b"]); chk("short batch -> None, not half a result", v is None, n)
reset(); E.VOYAGE_API_KEY="k"
def boom(*a, **k): raise Exception("connection reset")
E.requests = types.SimpleNamespace(post=boom)
v,n = E.embed(["a"]); chk("transport exception -> None, never raises", v is None and "failed" in n)

print("\n=== anchor sentiment ===")
reset()
r=E.score_sentiment(["x"]); chk("no backend: unavailable, not neutral",
    r["available"] is False and r["labels"]=={}, str(r["reason"]))
chk("empty input handled", E.score_sentiment([])["available"] is False)

# A fake space where positive/negative/neutral are separable axes.
reset(); E.VOYAGE_API_KEY="k"
def axis_post(url, headers=None, json=None, timeout=None):
    out=[]
    for i,t in enumerate(json["input"]):
        if any(w in t for w in ("good","welcome","praise","relief","خوب","جيد","טובות")): vec=[1,0,0]
        elif any(w in t for w in ("disaster","outrage","condemnation","fear","فاجعه","كارثة","קטסטרופה")): vec=[0,1,0]
        elif any(w in t for w in ("factual","procedural","واقعی","وقائعي","עובדתי")): vec=[0,0,1]
        elif "HAPPY" in t: vec=[1,0,0]
        elif "ANGRY" in t: vec=[0,1,0]
        elif "PLAIN" in t: vec=[0,0,1]
        else: vec=[1,1,1]     # equidistant -> must abstain
        out.append({"index":i,"embedding":[float(x) for x in vec]})
    return R({"data":out})
E.requests = types.SimpleNamespace(post=axis_post)
r=E.score_sentiment(["HAPPY news","ANGRY news","PLAIN news","AMBIGUOUS"])
chk("available", r["available"] is True)
chk("positive found", r["labels"].get(0)=="positive", str(r["labels"]))
chk("negative found", r["labels"].get(1)=="negative", str(r["labels"]))
chk("neutral found", r["labels"].get(2)=="neutral", str(r["labels"]))
chk("ABSTAINS rather than guessing", 3 not in r["labels"] and r["abstained"]==1,
    f"abstained={r['abstained']}")
chk("abstention is not a neutral label", r["labels"].get(3) is None)
chk("names itself as a separate engine", r["engine"]=="anchor")
chk("caveat states it is the weaker engine", "never" in r["caveat"] and "override" in r["caveat"])
chk("reuses supplied vectors with no network",
    E.score_sentiment(["HAPPY"], vectors=[[1.0,0,0]])["labels"]=={0:"positive"})
reset()

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
