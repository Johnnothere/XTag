import sys, relevance as R
P=F=0
def chk(n,c,e=""):
    global P,F
    if c: P+=1; print(f"  PASS  {n}"+(f"  [{e}]" if e else ""))
    else: F+=1; print(f"  FAIL  {n}  {e}")

spec = R.plan_query("#covid1948")
def d(**kw):
    base = {"title":None,"excerpt":"","author":None,"url":"","meta":None}
    base.update(kw); return base

print("\n=== the crash ===")
try:
    sc,_ = R.score_doc(d(title="hello", meta={"views":100,"likes":5}), spec)
    chk("dict meta no longer raises", True, f"score={sc}")
except TypeError as e:
    chk("dict meta no longer raises", False, str(e))

for label, m in (("None",None),("str","#covid1948"),("dict",{"a":"x"}),
                 ("nested list",{"tags":["covid1948","x"],"n":3}),
                 ("odd types",{"o":object(),"n":1.5,"b":True}),("list",[1,2])):
    try:
        R.score_doc(d(meta=m), spec); chk(f"meta={label} survives", True)
    except Exception as e:
        chk(f"meta={label} survives", False, f"{type(e).__name__}: {e}")

print("\n=== meta text IS matched ===")
sc,b = R.score_doc(d(title="untitled", meta={"hashtags":["covid1948"]}), spec)
chk("hashtag in meta matches", sc >= R.EXACT, f"{sc} {b}")
sc,b = R.score_doc(d(title="untitled", meta={"channel":"COVID-1948 News"}), spec)
chk("channel name in meta matches", sc >= R.EXACT, f"{sc} {b}")

print("\n=== engagement counts must NOT create matches ===")
spec48 = R.plan_query("1948")
sc,b = R.score_doc(d(title="cat video", meta={"views":1948,"likes":1948}), spec48)
chk("view count of 1948 does not match query 1948", sc == R.NO_MATCH, f"{sc} {b}")
sc,b = R.score_doc(d(title="the 1948 war", meta={"views":10}), spec48)
chk("but real text still matches", sc >= R.EXACT, f"{sc} {b}")

print("\n=== negative controls still hold (Pass A regression) ===")
c19 = R.plan_query("covid19"); c48 = R.plan_query("#covid1948")
sc,_ = R.score_doc(d(title="COVID-1948 protest"), c19)
chk("covid19 does NOT match covid1948", sc == R.NO_MATCH, str(sc))
sc,_ = R.score_doc(d(title="covid19 vaccine"), c48)
chk("covid1948 does NOT match covid19", sc == R.NO_MATCH, str(sc))
sc,_ = R.score_doc(d(title="#COVID1948 in Tehran"), c48)
chk("exact still matches", sc >= R.EXACT, str(sc))
sc,_ = R.score_doc(d(title="COVİD1948"), c48)
chk("Turkish dotted I folds", sc >= R.EXACT, str(sc))

print(f"\n{'='*46}\n  {P} passed, {F} failed\n{'='*46}")
sys.exit(1 if F else 0)
