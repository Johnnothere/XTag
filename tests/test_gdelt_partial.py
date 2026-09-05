import sys; sys.path.insert(0,".")
import json, logging, gdelt as G
logging.getLogger("gdelt").setLevel(logging.CRITICAL)   # expected failures are noise here
P=F=0
def chk(n,c,e=""):
    global P,F
    if c:P+=1;print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else:F+=1;print(f"  FAIL  {n}  {e}")

# ── HTTP mock ─────────────────────────────────────────────────────────────────
# gdelt._get is the module's single choke point for every request, so replacing
# it is the whole network layer. NOTHING here touches api.gdeltproject.org.
CALLS=[]
def stub(script):
    """script: list of (data, error) — one entry per window, last one repeats."""
    def _get(path, params):
        CALLS.append(params)
        return script[min(len(CALLS)-1, len(script)-1)]
    return _get

def arts(n, tag="w"):
    """One successful window: (data, error) exactly as _get returns it."""
    return ({"articles":[{"url":f"https://x.test/{tag}/{i}","title":f"t{i}",
                          "domain":"x.test","language":"English",
                          "sourcecountry":"United States"} for i in range(n)]}, None)

def run(script, windows=3, q="hezbollah"):
    CALLS.clear()
    G._get = stub(script)
    return G.articles(q, windows=windows)

_REAL_GET = G._get

print("\n=== a clean run says so, positively ===")
a, err = run([arts(10,"a"), arts(10,"b"), arts(10,"c")])
cov = G.coverage(a)
chk("no error", err is None, str(err))
chk("complete is True", cov["complete"] is True, json.dumps(cov))
chk("partial is False", cov["partial"] is False)
chk("all 3 windows fetched", cov["windows_ok"]==3 and cov["windows_total"]==3,
    f"ok={cov['windows_ok']}/{cov['windows_total']}")
chk("nothing failed, abandoned or truncated",
    (cov["windows_failed"],cov["windows_abandoned"],cov["windows_truncated"])==(0,0,0), str(cov))
chk("30 articles", len(a)==30 and cov["articles"]==30, str(len(a)))
chk("no reason to report", cov["reason"] is None, str(cov["reason"]))

print("\n=== an empty-but-healthy corpus is still complete ===")
# 204 / empty body -> _get returns (None, None). Zero news is not zero coverage.
a, err = run([(None,None)]*3)
chk("no error on genuinely empty windows", err is None, str(err))
chk("complete despite zero articles", G.coverage(a)["complete"] is True, json.dumps(G.coverage(a)))
chk("windows counted as fetched", G.coverage(a)["windows_ok"]==3)

print("\n=== truncation at the 250-record cap is reported ===")
a, err = run([arts(G.MAX_RECORDS,"a"), arts(G.MAX_RECORDS,"b"), arts(5,"c")])
cov = G.coverage(a)
chk("returns an error even though articles came back", err is not None and len(a)>0, str(len(a)))
chk("error kind is 'partial'", G.error_kind(err)=="partial", G.error_kind(err))
chk("complete is False", cov["complete"] is False, json.dumps(cov))
chk("partial is True", cov["partial"] is True)
chk("2 truncated windows", cov["windows_truncated"]==2, str(cov["windows_truncated"]))
chk("truncation named in the message", "truncated" in str(err).lower(), str(err)[:70])
chk("err carries the same coverage record", G.coverage(err)==cov)
chk("app.py's flat attrs still populated",
    (getattr(err,"windows_total",None),getattr(err,"windows_failed",None),
     getattr(err,"windows_truncated",None))==(3,0,2))

print("\n=== a window that fails is not a window with no news ===")
a, err = run([arts(4,"a"), (None,G.GdeltError("timed out after 12s","timeout")), arts(4,"c")])
cov = G.coverage(a)
chk("reported partial", G.error_kind(err)=="partial" and cov["partial"] is True, str(err)[:60])
chk("1 failed, 2 ok", (cov["windows_failed"],cov["windows_ok"])==(1,2), str(cov))
chk("a timeout does NOT abandon the rest", cov["windows_abandoned"]==0 and len(CALLS)==3,
    f"{len(CALLS)} calls")

print("\n=== windows abandoned by the breaker are counted (A14) ===")
a, err = run([arts(7,"a"),
              (None,G.GdeltError("backing off after repeated connection failures","breaker"))])
cov = G.coverage(a)
chk("loop actually stopped", len(CALLS)==2, f"{len(CALLS)} requests made")
chk("complete is False", cov["complete"] is False, json.dumps(cov))
chk("1 window never attempted", cov["windows_abandoned"]==1, str(cov["windows_abandoned"]))
chk("abandonment is in the message", "never attempted" in str(err), str(err)[:90])
chk("every window accounted for exactly once",
    cov["windows_ok"]+cov["windows_failed"]+cov["windows_abandoned"]==cov["windows_total"],
    json.dumps(cov))
chk("the pre-A14 read would have over-reported coverage",
    cov["windows_failed"]==1 and cov["windows_abandoned"]==1,
    "1 of 3 'failed' but only 1 of 3 fetched")
chk("err exposes windows_abandoned", getattr(err,"windows_abandoned",None)==1)

print("\n=== a malformed window is not a silent one ===")
a, err = run([arts(3,"a"), (["not","a","dict"],None), arts(3,"c")])
cov = G.coverage(a)
chk("counted as a failure, not skipped", cov["windows_failed"]==1, json.dumps(cov))
chk("not reported complete", cov["complete"] is False)
chk("says what happened", "unexpected response shape" in str(err), str(err)[:80])

print("\n=== total failure is 'failed', not 'partial' ===")
a, err = run([(None,G.GdeltError("http 503","http"))]*3)
cov = G.coverage(a)
chk("kind is 'failed'", G.error_kind(err)=="failed", G.error_kind(err))
chk("partial is False with nothing to be partial about", cov["partial"] is False, json.dumps(cov))
chk("complete is False", cov["complete"] is False)

print("\n=== the empty query still reports coverage ===")
a, err = run([arts(5,"a")], q="")
chk("errors out", bool(err), str(err))
chk("no request made", len(CALLS)==0, f"{len(CALLS)} calls")
chk("does not claim completeness", G.coverage(a)["complete"] is False, json.dumps(G.coverage(a)))

print("\n=== ArticleSet stays a list for every existing consumer ===")
a, err = run([arts(2,"a"), arts(2,"b"), arts(2,"c")])
chk("isinstance list", isinstance(a, list))
chk("iterates", [d["url"] for d in a][:1]==["https://x.test/a/0"])
chk("len / truthiness", len(a)==6 and bool(a))
chk("json serialisable", json.loads(json.dumps(a))[0]["domain"]=="x.test")
chk("slices to a plain list", isinstance(a[:2], list) and len(a[:2])==2)
chk("unpacks as a 2-tuple like before", isinstance(G.articles("x", windows=1), tuple))
chk("coverage on a bare list is never a false 'complete'",
    G.coverage(["raw"])["complete"] is False)

print("\n=== dedupe across windows is unaffected ===")
a, err = run([arts(5,"same"), arts(5,"same"), arts(5,"same")])
chk("identical urls collapse", len(a)==5, str(len(a)))
chk("coverage counts the deduped total", G.coverage(a)["articles"]==5)

# ── snapshot() ────────────────────────────────────────────────────────────────
def tl(series):
    return {"timeline":[{"series":s,"data":[{"date":"20260822T170000Z","value":v}]}
                        for s,v in series]}

def snap_stub(fail_mode=None):
    def _get(path, params):
        mode = params.get("mode")
        if mode == fail_mode:
            return None, G.GdeltError("rate limited (429)","rate_limit")
        if mode == "timelinetone":      return tl([("Average Tone",-3.2)]), None
        if mode == "timelinevolraw":    return tl([("Volume",120.0)]), None
        if mode == "timelinesourcecountry":
            return tl([("United States Volume Intensity",9.0),
                       ("Lebanon Volume Intensity",4.0)]), None
        if mode == "timelinelang":
            return tl([("English Volume Intensity",9.0),("Arabic Volume Intensity",3.0)]), None
        return None, None
    return _get

print("\n=== snapshot() states completeness instead of implying it ===")
G._cache.clear()
G._get = snap_stub()
s = G.snapshot("hezbollah-clean")
chk("no degraded stages", s["degraded"]==[], str(s["degraded"]))
chk("complete is True", s["complete"] is True)
chk("partial is False", s["partial"] is False)
chk("4 of 4 stages ok", (s["stages_ok"],s["stages_total"])==(4,4), f"{s['stages_ok']}/{s['stages_total']}")

G._cache.clear()
G._get = snap_stub(fail_mode="timelinesourcecountry")
s = G.snapshot("hezbollah-degraded")
chk("degraded names the stage", any("geography" in d for d in s["degraded"]), str(s["degraded"]))
chk("complete is False", s["complete"] is False)
chk("partial is True — some stages did land", s["partial"] is True)
chk("3 of 4 stages ok", s["stages_ok"]==3, str(s["stages_ok"]))
chk("still returns the stages that worked", bool(s["tone"].get("points")))

chk("flags survive the cache round-trip",
    G.snapshot("hezbollah-degraded")["complete"] is False and
    G.snapshot("hezbollah-degraded")["cached"] is True)

G._get = _REAL_GET

print("\n=== the breaker must survive interleaved successes ===")
# The measured failure: GDELT timed out on most calls but succeeded on some, so
# _record_success() zeroed the counter every time and the breaker never fired —
# every search paid 45s for a source that reported itself failed.
import gdelt as G
G._consecutive_failures = 0; G._breaker_open_until = 0.0
for _ in range(3): G._record_failure(True)
chk("3 straight failures open the breaker", G._breaker_is_open())

G._consecutive_failures = 0; G._breaker_open_until = 0.0
for _ in range(2): G._record_failure(True)
G._record_success()
for _ in range(2): G._record_failure(True)
chk("fail,fail,ok,fail,fail STILL opens it", G._breaker_is_open(),
    f"consecutive={G._consecutive_failures} — zeroing here is the bug")

G._consecutive_failures = 0; G._breaker_open_until = 0.0
for _ in range(2): G._record_failure(True)
for _ in range(5): G._record_success()
chk("a healthy source walks back to zero", G._consecutive_failures == 0,
    str(G._consecutive_failures))
G._record_failure(True)
chk("...and does not trip on one later blip", not G._breaker_is_open())
G._consecutive_failures = 0; G._breaker_open_until = 0.0

print("\n=== interactive budget ===")
chk("REQ_TIMEOUT bounded for the hot path", G.REQ_TIMEOUT <= 8, f"{G.REQ_TIMEOUT}s")
chk("one window by default", G.ARTICLE_WINDOWS == 1, str(G.ARTICLE_WINDOWS))
chk("worst case is one timeout, not three",
    G.REQ_TIMEOUT * G.ARTICLE_WINDOWS <= 8,
    f"{G.REQ_TIMEOUT * G.ARTICLE_WINDOWS}s was 36s")
chk("depth still available on request",
    "windows" in __import__("inspect").signature(G.articles).parameters)

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
