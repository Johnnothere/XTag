import time, sys, threading
from concurrent.futures import ThreadPoolExecutor
import app
P=F=0
def chk(n,c,e=""):
    global P,F
    if c: P+=1; print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else: F+=1; print(f"  FAIL  {n}  {e}")

print("\n=== A. the GDELT future survives the pool shutdown ===")
# Exactly the shape in _run_full_search: gdelt submitted FIRST into an empty
# pool (so it starts immediately), collection submitted after, then a
# non-blocking shutdown with cancel_futures=True.
N = len(app.API_PLATFORMS) + 2
ex = ThreadPoolExecutor(max_workers=N)
started = threading.Event()
def slow_gdelt():
    started.set(); time.sleep(1.2); return {"volume": 42}
gf = ex.submit(slow_gdelt)
others = [ex.submit(time.sleep, 0.01) for _ in range(len(app.API_PLATFORMS)+1)]
time.sleep(0.15)
ex.shutdown(wait=False, cancel_futures=True)
chk("gdelt task had started", started.is_set())
chk("gdelt future not cancelled", not gf.cancelled())
t0=time.monotonic(); res = gf.result(timeout=5); el=time.monotonic()-t0
chk("gdelt result readable after shutdown", res == {"volume": 42}, f"{res} in {el:.2f}s")

print("\n=== B. an over-subscribed pool: the LAST submit can be cancelled ===")
# Proof the ordering matters — this is why gdelt is submitted first.
ex2 = ThreadPoolExecutor(max_workers=1)
ex2.submit(time.sleep, 1.0)
late = ex2.submit(lambda: "never")
ex2.shutdown(wait=False, cancel_futures=True)
chk("a queued (unstarted) future IS cancelled", late.cancelled(),
    "confirms submit-order is load-bearing")

print("\n=== C. concurrency actually overlaps ===")
t0 = time.monotonic()
ex3 = ThreadPoolExecutor(max_workers=4)
g = ex3.submit(time.sleep, 0.8)               # the 'snapshot'
c = [ex3.submit(time.sleep, 0.8) for _ in range(3)]   # 'collection'
for f in c: f.result(timeout=5)
g.result(timeout=5)
ex3.shutdown(wait=False)
el = time.monotonic()-t0
chk("snapshot hides inside collection", el < 1.3,
    f"{el:.2f}s wall for 4x0.8s concurrent (sequential would be 3.2s)")

print("\n=== D. P1-8: thin corpus is not an engine failure ===")
import inspect
src = inspect.getsource(app._run_full_search)
chk("guard checks corpus size", "_kept_docs < MIN_ENTITY_DOCS" in src)
chk("thin path sets insufficient_evidence", 'insufficient_evidence' in src)
chk("thin path does NOT set engine_errors", 
    src.index('insufficient_evidence') < src.index('engine_errors["entities"] = ('))
chk("real failure path retained", 'engine_errors["entities"] = (' in src)

print("\n=== E. timings land on the payload ===")
chk("payload carries timings", 'payload["timings"]' in src)
chk("timings sorted slowest-first", 'key=lambda kv: -kv[1]' in src)
chk("total recorded", 'budget.record("total"' in src)
for stage in ("collection","relevance","languages","sentiment","aggregates",
              "assessment","gdelt_snapshot_wait"):
    chk(f"stage timed: {stage}", f'"{stage}"' in src)

print("\n=== F. every stage timeout is clamped to the budget ===")
chk("collection clamped", "budget.slice(SEARCH_POOL_TIMEOUT" in src)
chk("analysis clamped", "budget.slice(ANALYSIS_TIMEOUT" in src)
chk("gdelt wait clamped", "budget.slice(20" in src)
chk("analysis uses one shared deadline", "_an_end" in src and "analysis budget exhausted" in src)
chk("no per-future analysis timeouts left", "f_narr,ANALYSIS_TIMEOUT" not in src)

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
