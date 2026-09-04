import sys, coordination as C, harness
P=F=0
def chk(n,c,e=""):
    global P,F
    if c:P+=1;print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else:F+=1;print(f"  FAIL  {n}  {e}")

print("\n=== URL canonicalisation ===")
same=[("https://a.com/x?utm_source=tw&id=3","https://www.a.com/x?id=3"),
      ("http://AMP.a.com/x/","https://a.com/x"),
      ("https://a.com/x?b=1&a=2","https://a.com/x?a=2&b=1"),
      ("https://a.com/x#frag","https://a.com/x"),
      ("https://a.com:443/x","https://a.com/x"),
      ("https://a.com/x?fbclid=zz","https://a.com/x")]
for u,v in same:
    chk(f"{u[:34]:<34} == {v[:26]}", C.canonical_url(u)==C.canonical_url(v),
        f"{C.canonical_url(u)} vs {C.canonical_url(v)}")
chk("different paths stay different", C.canonical_url("https://a.com/x")!=C.canonical_url("https://a.com/y"))
chk("meaningful query preserved", "id=3" in C.canonical_url("https://a.com/x?id=3"))
for bad in ("", None, "javascript:alert(1)", "not a url", 12345, "ftp://a.com/x"):
    chk(f"rejects {bad!r}", C.canonical_url(bad)=="" or C.canonical_url(bad).startswith("https://"))

print("\n=== robustness against real-world document shapes ===")
for docs,label in (([], "empty"), ([{}]*20, "empty dicts"),
                   ([{"url":None,"title":None,"excerpt":None}]*20,"all null fields"),
                   ([{"platform":"x"}]*3,"below minimum"),
                   ([{"platform":"x","title":"a b c","url":"x","author":f"u{i}",
                      "timestamp":"garbage"} for i in range(20)],"unparseable timestamps")):
    try:
        r=C.detect(docs); chk(f"survives {label}", isinstance(r,dict) and "coordination_score" in r)
    except Exception as e:
        chk(f"survives {label}", False, f"{type(e).__name__}: {e}")

print("\n=== refuses to band without a baseline ===")
c=harness.build("copypasta",32,400,seed=7)
docs=[r for g in c.platforms().values() for r in g["results"]]
r=C.detect(docs)
chk("unbanded without baseline", r["risk"]=="unbanded", r["risk"])
chk("says why", "baseline" in (r.get("caveat") or "").lower())
chk("ratio is None", r["baseline_ratio"] is None)
r2=C.detect(docs, baseline=5.0)
chk("bands with a baseline", r2["risk"] in ("low","medium","high"), r2["risk"])
chk("reports the multiple", r2["baseline_ratio"] is not None, str(r2["baseline_ratio"]))
r3=C.detect(docs, baseline=0.0)
chk("zero baseline -> undefined, not infinity", r3["baseline_ratio"] is None and r3["risk"]=="unbanded")

print("\n=== the disparity escape hatch ===")
clique={(f"a{i}",f"a{j}"):1.0 for i in range(12) for j in range(i+1,12)}
chk("a uniform clique survives filtering", len(C.disparity_filter(clique))==len(clique),
    f"{len(C.disparity_filter(clique))}/{len(clique)}")
chk("...and would NOT without the hatch",
    len(C.disparity_filter(clique, keep_above=2.0)) < len(clique)*0.2,
    f"{len(C.disparity_filter(clique, keep_above=2.0))}/{len(clique)} — the bug this fixes")
weak={("a","b"):0.05,("a","c"):0.05,("a","d"):0.05,("a","e"):0.05}
chk("weak uniform edges still filtered", len(C.disparity_filter(weak))==0, str(len(C.disparity_filter(weak))))

print("\n=== deterministic ===")
chk("same input, same output", C.detect(docs)==C.detect(docs))

print("\n=== measured performance (regression guards) ===")
def nd(p): return C.detect([r for g in p.values() for r in (g.get("results") or [])])
for kind in harness.CAMPAIGNS:
    r=harness.evaluate(nd,kind,32)
    chk(f"{kind:<14} recall>=90% prec>=85%", r.recall>=0.90 and r.precision>=0.85,
        f"r={r.recall:.0%} p={r.precision:.0%}")
org=[nd(harness.build("copypasta",0,400,seed=400+s).platforms())["coordination_score"] for s in range(5)]
chk("organic-only scores 0", max(org)==0, str(org))

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
