import time, sys, os
import app
P=F=0
def chk(n,c,e=""):
    global P,F
    if c: P+=1; print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else: F+=1; print(f"  FAIL  {n}  {e}")

def doc(pid, i, txt):
    return {"platform":pid,"title":txt,"excerpt":txt,
            "url":f"https://{pid}.example/{i}","timestamp":"2026-08-01T00:00:00Z",
            "meta":{"likes":i}}

# ── mock every upstream ─────────────────────────────────────────────────────
def mk(pid, n, txt):
    def f(q):
        time.sleep(0.05)
        return {"platform":pid,"results":[doc(pid,i,txt) for i in range(n)],"error":None}
    return f

app.API_PLATFORMS = {
    "x":      mk("x", 12, "#covid1948 protest in Tehran today"),
    "youtube":mk("youtube", 30, "covid1948 explained"),
    "slow":   (lambda q: (time.sleep(30), {"platform":"slow","results":[],"error":None})[1]),
}
app.search_serpapi = lambda q: {}
app.gdelt.snapshot = lambda q: (time.sleep(0.3), {"volume":{"n":5}})[1]
app.ANTHROPIC_API_KEY = ""      # no Claude on the hot path
app.BABELSTREET_API_KEY = ""
app.SEARCH_POOL_TIMEOUT = 3     # so the 'slow' source must be abandoned

print("\n=== G. a full run completes and reports where time went ===")
t0 = time.monotonic()
r = app._run_full_search("#covid1948", use_cache=False)
el = time.monotonic() - t0

chk("returns a payload", isinstance(r, dict) and r.get("query") == "#covid1948")
chk("a 30s source did not hold the request", el < 8, f"wall {el:.2f}s with a 30s hung source")
chk("hung source marked timed out",
    (r["platforms"].get("slow") or {}).get("error") == "timed out",
    str((r["platforms"].get("slow") or {}).get("error")))
chk("good sources survived", len(r["platforms"]["x"]["results"]) > 0)

t = r.get("timings") or {}
chk("timings present", bool(t), str(t))
chk("timings include total", "total" in t)
chk("timings include collection", "collection" in t)
chk("total ~= wall clock", abs(t.get("total",0) - el) < 1.0, f"total={t.get('total')} wall={el:.2f}")
chk("sorted slowest first", list(t.values()) == sorted(t.values(), reverse=True), str(list(t)[:4]))

print("\n=== H. GDELT overlapped rather than tailed ===")
gw = t.get("gdelt_snapshot_wait", 999)
chk("snapshot wait is near zero", gw < 0.35,
    f"{gw}s — the 0.3s snapshot ran inside collection, not after it")
chk("snapshot data present", (r.get("gdelt") or {}).get("volume") == {"n":5}, str(r.get("gdelt"))[:60])

print("\n=== I. thin corpus reports evidence, not a fault ===")
app.API_PLATFORMS = {"x": mk("x", 2, "#covid1948")}
r2 = app._run_full_search("#covid1948 thin", use_cache=False)
ee = r2.get("engine_errors") or {}
ents = r2.get("entities") or {}
chk("no bogus entities engine error", "entities" not in ee, str(ee))
chk("explains thin evidence instead", "insufficient_evidence" in ents, str(ents)[:120])

print("\n=== J. degraded results still not cached (P0 held) ===")
chk("degraded key present", "degraded" in r)

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
