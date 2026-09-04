import sys, harness
P=F=0
def chk(n,c,e=""):
    global P,F
    if c: P+=1; print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else: F+=1; print(f"  FAIL  {n}  {e}")

print("\n=== ground truth is correct ===")
c = harness.build("copypasta", 32, 400, seed=7)
allu = [d.url for d in c.docs]
chk("corpus size", len(c.docs)==432, str(len(c.docs)))
chk("labels match count", len(c.injected_urls)==32)
chk("urls unique", len(allu)==len(set(allu)))
chk("injected flagged on the doc", sum(1 for d in c.docs if d.injected)==32)
chk("no organic doc labelled injected",
    all(not d.injected for d in c.docs if d.url.startswith("https://organic")))
chk("injected urls all present in corpus", c.injected_urls <= set(allu))
chk("deterministic", harness.build("copypasta",32,400,seed=7).injected_urls==c.injected_urls)
chk("different seed differs", harness.build("copypasta",32,400,seed=8).injected_urls!=c.injected_urls)

print("\n=== the scorer is calibrated against known detectors ===")
def perfect(plats):
    urls=[r["url"] for g in plats.values() for r in g["results"] if "camp.example" in r["url"] or "target.example" in r["url"] or "/p?ref=" in r["url"]]
    return {"signals":[{"type":"t","examples":urls}],"coordination_score":99,"risk":"high"}
def blind(plats):
    return {"signals":[],"coordination_score":0,"risk":"low"}
def liar(plats):   # flags everything
    urls=[r["url"] for g in plats.values() for r in g["results"]]
    return {"signals":[{"type":"t","examples":urls}],"coordination_score":99,"risk":"high"}

for kind in harness.CAMPAIGNS:
    r = harness.evaluate(perfect, kind, 32)
    chk(f"oracle scores 100% on {kind}", r.recall==1.0 and r.precision==1.0,
        f"recall {r.recall:.0%} prec {r.precision:.0%}")
r = harness.evaluate(blind, "copypasta", 32)
chk("null detector: 0 recall", r.recall==0.0)
chk("null detector: negative SMISC", r.smisc < 0, f"{r.smisc:.1f}")
r = harness.evaluate(liar, "copypasta", 32)
chk("flag-everything gets full recall", r.recall==1.0)
chk("...but is punished on precision", r.precision < 0.10, f"{r.precision:.1%}")

print("\n=== breaking_point behaves ===")
chk("oracle breaks at the smallest size",
    harness.breaking_point(harness.sweep(perfect,"copypasta"))==4)
chk("blind detector never breaks",
    harness.breaking_point(harness.sweep(blind,"copypasta")) is None)

print("\n=== baseline_ratio is honest about a zero baseline ===")
b = harness.baseline_ratio(blind, "copypasta", 32, trials=2)
chk("no divide-by-zero ratio", b["ratio"] is None, str(b["ratio"]))
chk("says so explicitly", "undefined" in b["note"])
b2 = harness.baseline_ratio(perfect, "copypasta", 32, trials=2)
chk("constant detector does not separate", b2["separated"] is False, str(b2))

print("\n=== campaigns differ from each other and from organic ===")
texts = {}
for kind in harness.CAMPAIGNS:
    cc = harness.build(kind, 16, 0, seed=3)
    texts[kind] = {d.title for d in cc.docs}
chk("copypasta is one repeated string", len(texts["copypasta"])==1)
chk("paraphrase varies wording", len(texts["paraphrase"])>=5, str(len(texts["paraphrase"])))
chk("adaptive varies wording", len(texts["adaptive"])>=5, str(len(texts["adaptive"])))
sp = harness.build("same_platform",16,0,seed=3)
chk("same_platform is single-platform", len({d.platform for d in sp.docs})==1)
cp = harness.build("copypasta",16,0,seed=3)
chk("copypasta is multi-platform", len({d.platform for d in cp.docs})>2)
uv = harness.build("url_variants",16,0,seed=3)
chk("url_variants share a host, differ in query",
    len({d.url.split("?")[0] for d in uv.docs})==1 and len({d.url for d in uv.docs})==16)
sb = harness.build("slow_burn",16,0,seed=3)
import datetime as _dt
span = lambda cc: (max(_dt.datetime.fromisoformat(d.timestamp.replace("Z","+00:00")) for d in cc.docs)
                 - min(_dt.datetime.fromisoformat(d.timestamp.replace("Z","+00:00")) for d in cc.docs))
chk("slow_burn spans days, copypasta minutes",
    span(sb) > _dt.timedelta(hours=12) and span(cp) < _dt.timedelta(hours=2),
    f"slow {span(sb)}, fast {span(cp)}")

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
