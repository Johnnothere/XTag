import sys, random
sys.path.insert(0,".")
import narratives as N
P=F=0
def chk(n,c,e=""):
    global P,F
    if c:P+=1;print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else:F+=1;print(f"  FAIL  {n}  {e}")

print("\n=== Hungarian assignment ===")
chk("trivial 1x1", N.hungarian([[0.0]])==[(0,0)])
c=[[1,9,9],[9,1,9],[9,9,1]]
chk("diagonal", sorted(N.hungarian(c))==[(0,0),(1,1),(2,2)], str(sorted(N.hungarian(c))))
# the case greedy gets wrong: row0 prefers col0 but the optimum gives it col1
c=[[1,2],[3,9]]
got=sorted(N.hungarian(c)); tot=sum(c[i][j] for i,j in got)
chk("beats greedy (optimal total)", tot==min(1+9,2+3), f"pairs {got} total {tot}")
chk("rectangular wide", len(N.hungarian([[1,2,3],[4,5,6]]))==2)
chk("rectangular tall", len(N.hungarian([[1,2],[3,4],[5,6]]))==2)
chk("empty", N.hungarian([])==[])
r=random.Random(0)
for _ in range(20):
    n,m=r.randint(1,5),r.randint(1,5)
    M=[[r.random() for _ in range(m)] for _ in range(n)]
    pr=N.hungarian(M)
    ok=len(pr)==min(n,m) and len({i for i,_ in pr})==len(pr) and len({j for _,j in pr})==len(pr)
    if not ok: break
chk("random matrices: valid one-to-one assignment", ok)

print("\n=== DP-means ===")
v=N.tfidf(["vaccine data was falsified by the ministry",
           "the ministry falsified the vaccine data",
           "vaccine data falsified ministry report",
           "local transport funding increased this quarter",
           "transport funding for the region increased"])
g=N.dp_means(v)
chk("separates two topics", len(g)==2, f"{len(g)} clusters: {g}")
chk("largest cluster first", len(g[0])>=len(g[-1]))
chk("every point assigned once", sorted(i for c in g for i in c)==list(range(5)))
chk("deterministic", N.dp_means(v)==g)
chk("empty input", N.dp_means([])==[])
chk("higher join_sim -> more clusters",
    len(N.dp_means(v,join_sim=0.90))>=len(N.dp_means(v,join_sim=0.20)),
    f"{len(N.dp_means(v,join_sim=0.90))} vs {len(N.dp_means(v,join_sim=0.20))}")
chk("lam back-compat still accepted", isinstance(N.dp_means(v,lam=0.10),list))
# the measured limit, asserted so a future change to the tokeniser surfaces it
vv=N.tfidf(["the ministry falsified vaccine data",
            "ministry accused of falsifying vaccination figures",
            "local transport funding rose"])
chk("paraphrase similarity is genuinely low under TF-IDF",
    N.cosine(vv[0],vv[1])<0.30, f"{N.cosine(vv[0],vv[1]):.2f} — why embeddings are the upgrade")
chk("...but still above unrelated", N.cosine(vv[0],vv[1])>N.cosine(vv[0],vv[2]),
    f"{N.cosine(vv[0],vv[1]):.2f} vs {N.cosine(vv[0],vv[2]):.2f}")

print("\n=== identity survives across observations ===")
c1=[{"label":"data falsified","members":["u1","u2","u3","u4"]},
    {"label":"transport funding","members":["u5","u6","u7"]}]
s1=N.track(None,c1,"2026-08-01T00:00:00Z")
ids1={n["label"]:n["id"] for n in s1["narratives"]}
chk("two births", sum(1 for e in s1["events"] if e["kind"]=="birth")==2)
chk("ids are stable-looking", all(n["id"].startswith("n_") for n in s1["narratives"]))

# same narratives, mostly same documents, one grew
c2=[{"label":"data falsified","members":["u1","u2","u3","u4","u8","u9"]},
    {"label":"transport funding","members":["u5","u6","u7"]}]
s2=N.track(s1["narratives"],c2,"2026-08-01T01:00:00Z")
ids2={n["label"]:n["id"] for n in s2["narratives"]}
chk("SAME id across observations", ids2["data falsified"]==ids1["data falsified"],
    f"{ids1['data falsified']} -> {ids2['data falsified']}")
chk("no spurious births", not [e for e in s2["events"] if e["kind"]=="birth"],
    str([e['kind'] for e in s2['events']]))
chk("growth reported", any(e["kind"]=="growth" for e in s2["events"]),
    str([e.get('detail') for e in s2['events']]))
n=[x for x in s2["narratives"] if x["label"]=="data falsified"][0]
chk("observations counted", n["observations"]==2, str(n["observations"]))
chk("first_seen preserved", n["first_seen"]=="2026-08-01T00:00:00Z")
chk("history accumulates", len(n["history"])==2, str(n["history"]))

print("\n=== a missing narrative is not immediately dead ===")
s3=N.track(s2["narratives"],[c2[0]],"t3")
gone=[x for x in s3["narratives"] if x["label"]=="transport funding"]
chk("survives one miss", gone and gone[0]["misses"]==1, str([ (x['label'],x['misses']) for x in s3['narratives']]))
chk("no death event yet", not any(e["kind"]=="death" for e in s3["events"]))
s4=N.track(s3["narratives"],[c2[0]],"t4")
s5=N.track(s4["narratives"],[c2[0]],"t5")
chk("dies after 3 misses", any(e["kind"]=="death" for e in s5["events"]),
    str([e['kind'] for e in s5['events']]))
chk("removed from state", not [x for x in s5["narratives"] if x["label"]=="transport funding"])
chk("returns after a miss keeps its id",
    [x for x in N.track(s3["narratives"],c2,"t4b")["narratives"]
     if x["label"]=="transport funding"][0]["id"]==ids1["transport funding"])

print("\n=== merge and split ===")
sp=N.track(s1["narratives"],
           [{"label":"data falsified A","members":["u1","u2"]},
            {"label":"data falsified B","members":["u3","u4"]},
            {"label":"transport funding","members":["u5","u6","u7"]}],"t")
chk("split detected", any(e["kind"]=="split" for e in sp["events"]),
    str([e['kind'] for e in sp['events']]))
mg=N.track(s1["narratives"],
           [{"label":"everything","members":["u1","u2","u3","u4","u5","u6","u7"]}],"t")
chk("merge detected", any(e["kind"]=="merge" for e in mg["events"]),
    str([e['kind'] for e in mg['events']]))

print("\n=== unrelated content gets a new identity, not a stolen one ===")
s=N.track(s1["narratives"],[{"label":"totally different","members":["z1","z2","z3"]}],"t")
new=[x for x in s["narratives"] if x["label"]=="totally different"][0]
chk("new narrative born", new["id"] not in ids1.values())
chk("birth event emitted", any(e["kind"]=="birth" for e in s["events"]))

print("\n=== round-trips through serialisation ===")
import json
st=s2["narratives"]
chk("json serialisable", json.loads(json.dumps(st))==st)
chk("re-tracking from serialised state works",
    N.track(json.loads(json.dumps(st)),c2,"t")["narratives"][0]["id"]==st[0]["id"])

print("\n=== cluster_claims ===")
cl=N.cluster_claims([{"text":"the ministry falsified vaccine data","url":"a"},
                     {"text":"vaccine data was falsified by the ministry","url":"b"},
                     {"text":"transport funding rose this quarter","url":"c"}])
chk("groups paraphrases", len(cl)==2, str([(c['label'],c['size']) for c in cl]))
chk("label is a real claim", any(c["label"].startswith("the ministry") or
                                 c["label"].startswith("vaccine") for c in cl))
chk("members carry urls", all(isinstance(c["members"],list) for c in cl))
chk("cohesion reported", all(0<=c["cohesion"]<=1 for c in cl))
for bad in ([], [{}], [{"text":""}], [{"text":"x","url":None}]):
    try: N.cluster_claims(bad); chk(f"survives {bad}", True)
    except Exception as e: chk(f"survives {bad}", False, str(e))

print("\n=== semantic drift ===")
a=N.track(None,[{"label":"vaccine data was falsified","members":["u1","u2","u3","u4"]}],"t1")
b=N.track(a["narratives"],[{"label":"transport funding rose sharply","members":["u1","u2","u3","u4"]}],"t2")
chk("drift reported when the subject changes",
    any(e["kind"]=="drift" for e in b["events"]), str([e["kind"] for e in b["events"]]))
chk("id is NOT reset by drift", b["narratives"][0]["id"]==a["narratives"][0]["id"])
c=N.track(a["narratives"],[{"label":"the vaccine data was falsified again","members":["u1","u2","u3","u4"]}],"t2")
chk("no drift on ordinary rewording", not any(e["kind"]=="drift" for e in c["events"]),
    str([e["kind"] for e in c["events"]]))
chk("no drift when the label is unchanged",
    not any(e["kind"]=="drift" for e in
            N.track(a["narratives"],[{"label":"vaccine data was falsified","members":["u1","u2","u3","u4"]}],"t2")["events"]))

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
