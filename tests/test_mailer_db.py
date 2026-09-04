import sys; sys.path.insert(0,".")
import re, logging, mailer as M, db as D
logging.getLogger("xtag.db").setLevel(logging.CRITICAL)   # expected rejections are noise here
P=F=0
def chk(n,c,e=""):
    global P,F
    if c:P+=1;print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else:F+=1;print(f"  FAIL  {n}  {e}")

def render(**kw):
    base=dict(query="Hezbollah", payload={"totals":{"mentions":1200},
              "sentiment":{"net":0.4}}, brief=None, history=None,
              unsubscribe_token=None, cadence_days=7)
    base.update(kw)
    return M.render_report(base["query"], base["payload"], base["brief"],
                           base["history"], base["unsubscribe_token"],
                           base["cadence_days"])

def tile(h,label):
    m=re.search(r'line-height:1;">([^<]*)</div><div[^>]*>'+label+r'</div>',h)
    return m.group(1) if m else None

def dcol(h,label):
    m=re.search(r'color:(#[0-9a-f]{6});background:#f7f6f2;border-radius:12px;'
                r'padding:3px 10px;margin-right:5px;">'+label+r' ',h)
    return m.group(1) if m else None

GREEN,RED,GREY="#0d9669","#dc2626","#71717a"
BANDS=("low risk","medium risk","high risk","unbanded risk","unknown risk")

print("\n=== delta colour: sentiment is the metric where UP is good ===")
h=render(history={"points":5,"change":{"mentions":300,"coordination_score":8,"sentiment_net":-0.42}})
chk("falling sentiment is RED, not green", dcol(h,"Sentiment")==RED, str(dcol(h,"Sentiment")))
chk("rising mentions is RED", dcol(h,"Mentions")==RED, str(dcol(h,"Mentions")))
chk("rising coordination is RED", dcol(h,"Coordination")==RED, str(dcol(h,"Coordination")))
h=render(history={"points":5,"change":{"mentions":-300,"coordination_score":-8,"sentiment_net":0.42}})
chk("rising sentiment is GREEN, not grey", dcol(h,"Sentiment")==GREEN, str(dcol(h,"Sentiment")))
chk("falling mentions is GREEN", dcol(h,"Mentions")==GREEN, str(dcol(h,"Mentions")))
chk("falling coordination is GREEN", dcol(h,"Coordination")==GREEN, str(dcol(h,"Coordination")))
h=render(history={"points":5,"change":{"mentions":0,"coordination_score":0,"sentiment_net":0.0}})
chk("no change is GREY for every metric",
    dcol(h,"Sentiment")==dcol(h,"Mentions")==dcol(h,"Coordination")==GREY,
    f"{dcol(h,'Sentiment')}/{dcol(h,'Mentions')}")
h=render(history={"points":5,"change":{"mentions":None,"coordination_score":None,"sentiment_net":None}})
chk("None deltas render nothing, not a colour", dcol(h,"Sentiment") is None)
chk("sentiment and mentions never share a colour for the same sign",
    dcol(render(history={"points":2,"change":{"mentions":5,"sentiment_net":5}}),"Sentiment") !=
    dcol(render(history={"points":2,"change":{"mentions":5,"sentiment_net":5}}),"Mentions"))

print("\n=== coordination: absent is not zero ===")
h=render(payload={"totals":{"mentions":1200},"sentiment":{"net":0.4}})
chk("absent coordination tile shows —, not 0", tile(h,"Coordination")=="—", str(tile(h,"Coordination")))
chk("says it was not assessed", "Not assessed for this report" in h)
chk("explicitly disclaims a zero finding", "not a finding of zero" in h)
chk("prints no band when absent", not any(b in h for b in BANDS))
chk("other tiles unaffected", tile(h,"Mentions")=="1,200", str(tile(h,"Mentions")))

print("\n=== coordination: unbanded must not be printed as a band ===")
CAV=("No matched organic baseline for this query, so this score is not banded.")
h=render(payload={"totals":{"mentions":9},"sentiment":{"net":0.1},
         "coordination":{"coordination_score":41,"risk":"unbanded","caveat":CAV}})
chk("no risk band rendered", not any(b in h for b in BANDS))
chk("the word 'unbanded' is never shown to the reader", "unbanded" not in h)
chk("score still shown in the tile", tile(h,"Coordination")=="41", str(tile(h,"Coordination")))
chk("says there is no baseline to compare against", "no matched organic baseline" in h.lower())
chk("prints the caveat verbatim", CAV in h)
h=render(payload={"totals":{"mentions":9},"coordination":
         {"coordination_score":0,"risk":"unknown","caveat":"4 documents — too few to assess coordination"}})
chk("risk 'unknown' also gets no band", not any(b in h for b in BANDS))
chk("...and its caveat is printed", "too few to assess coordination" in h)

print("\n=== coordination: a real band still renders ===")
for band,col in (("high","#dc2626"),("medium","#f97316"),("low","#0d9669")):
    h=render(payload={"coordination":{"coordination_score":63,"risk":band,"baseline_ratio":9.1}})
    chk(f"{band:<6} band shown with its colour", f"{band} risk</span>" in h and col in h)
    chk(f"{band:<6} does not claim a missing baseline", "no matched organic baseline" not in h.lower())
h=render(payload={"coordination":{"coordination_score":0,"risk":"low"}})
chk("a measured zero says so in words", "No coordinated actor clusters were found" in h)
chk("...and still shows 0 in the tile", tile(h,"Coordination")=="0", str(tile(h,"Coordination")))

print("\n=== coordination: hostile caveat is escaped ===")
h=render(payload={"coordination":{"coordination_score":3,"risk":"unbanded",
         "caveat":"<script>alert(1)</script>"}})
chk("caveat is HTML-escaped", "<script>" not in h and "&lt;script&gt;" in h)

print("\n=== end to end with the real detector ===")
try:
    import coordination as C, harness
    docs=[r for g in harness.build("copypasta",32,400,seed=7).platforms().values() for r in g["results"]]
    real=C.detect(docs)
    h=render(payload={"totals":{"mentions":len(docs)},"coordination":real})
    chk("detect() really returns unbanded by default", real["risk"]=="unbanded", real["risk"])
    chk("its report prints no band", not any(b in h for b in BANDS))
    chk("its report carries detect()'s own caveat", (real.get("caveat") or "?")[:40] in h)
    banded=C.detect(docs, baseline=5.0)
    hb=render(payload={"coordination":banded})
    chk("with a baseline the band appears", f"{banded['risk']} risk</span>" in hb, banded["risk"])
except ImportError as e:
    chk("coordination/harness importable", False, str(e))

print("\n=== db: PostgREST path parameters ===")
ok_ids=["9f3ca1b2c4d5","550e8400-e29b-41d4-a716-446655440000","abc_DEF-123","a"]
bad_ids=["x&or=(id.not.is.null)","x&select=*","abc&limit=1000","*","",
         "  ","1,2","a.b","x/y","%26or=(id.gt.0)","x?y","a"*65,None,True,4.5,
         b"abc","eq.null","x\nid=gt.0",["a"]]
for v in ok_ids: chk(f"accepts {v[:24]!r}", D._pk(v)==v)
for v in bad_ids: chk(f"rejects {str(v)[:24]!r}", D._pk(v) is None, str(D._pk(v)))
# bigserial ids come back from PostgREST as JSON numbers; rejecting them would
# have broken unsubscribe on schemas where report_subscriptions.id is a bigint.
chk("accepts an integer id", D._pk(4271)=="4271", str(D._pk(4271)))

calls=[]
def fake_req(method,path,**kw):
    calls.append((method,path)); return []
D._req=fake_req

calls.clear(); r=D.watchlist_delete("9f3ca1b2c4d5")
chk("valid id deletes", r is True and len(calls)==1, str(calls))
chk("...with exactly one eq filter", calls and calls[0][1]=="watchlists?id=eq.9f3ca1b2c4d5", str(calls))
EVIL="9f3c&or=(id.not.is.null)"
for name,fn,expect in (("watchlist_delete",lambda: D.watchlist_delete(EVIL),False),
                       ("watchlist_get",lambda: D.watchlist_get(EVIL),None),
                       ("watchlist_touch",lambda: D.watchlist_touch(EVIL,"now",{},[]),None),
                       ("subscription_deactivate",lambda: D.subscription_deactivate(EVIL),False),
                       ("subscription_mark_sent",lambda: D.subscription_mark_sent(EVIL,7,"sent"),None)):
    calls.clear(); got=fn()
    chk(f"{name:<23} refuses a crafted id", got is expect and calls==[], f"{got!r} {calls}")
calls.clear(); D.watchlist_get("../../rpc/exec")
chk("path traversal never reaches the wire", calls==[], str(calls))

print("\n=== db: unsubscribe token compared in constant time ===")
import inspect
chk("subscription_by_token uses compare_digest",
    "compare_digest" in inspect.getsource(D.subscription_by_token))
D._req=lambda m,p,**kw: [{"id":"s1","unsubscribe_token":"tok-abc","query":"q"}]
chk("matching token returns the row", (D.subscription_by_token("tok-abc") or {}).get("id")=="s1")
chk("near-miss token returns None", D.subscription_by_token("tok-abd") is None)
chk("prefix of the token returns None", D.subscription_by_token("tok-ab") is None)
calls.clear(); D._req=fake_req
chk("empty token makes no request", D.subscription_by_token("") is None and calls==[])
chk("non-string token makes no request", D.subscription_by_token(None) is None and calls==[])

print("\n=== db: cache pruning is wired, and off the hot path ===")
pruned=[]
D.cache_prune=lambda: pruned.append(1)
D.random.random=lambda: 0.0                      # dice always favourable
D._last_prune_at=0.0
chk("prunes once the interval has lapsed", D.maybe_prune(now=10_000.0) is True and len(pruned)==1)
chk("stamps the clock before the request", D._last_prune_at==10_000.0, str(D._last_prune_at))
chk("second call inside the interval does not", D.maybe_prune(now=10_001.0) is False and len(pruned)==1)
chk("nor at the interval boundary",
    D.maybe_prune(now=10_000.0+D._PRUNE_INTERVAL-1) is False and len(pruned)==1)
chk("but does once it has passed",
    D.maybe_prune(now=10_000.0+D._PRUNE_INTERVAL+1) is True and len(pruned)==2)
D.random.random=lambda: 0.99                     # dice unfavourable
D._last_prune_at=0.0
chk("unlucky roll skips even when due", D.maybe_prune(now=99_000.0) is False and len(pruned)==2)
chk("...and does not consume the time gate", D._last_prune_at==0.0, str(D._last_prune_at))
D.random.random=lambda: 0.0
D._last_prune_at=0.0; calls.clear(); pruned.clear()
D.cache_set("k","q",{"a":1},600)
chk("cache_set prunes", len(pruned)==1, str(len(pruned)))
chk("...after writing the row", len(calls)==1 and calls[0][0]=="POST", str(calls))
D._last_prune_at=0.0; pruned.clear()
D.cache_get("k")
chk("cache_get NEVER prunes (hot path stays one round-trip)", pruned==[], str(pruned))

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
