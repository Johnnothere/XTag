import collections, json, sys, adint
from killtest import walk_hosts, SOCIAL
NEWS = {"reuters.com","apnews.com","bbc.com","bbc.co.uk","cnn.com","nytimes.com",
"washingtonpost.com","wsj.com","ft.com","economist.com","theguardian.com",
"aljazeera.com","aljazeera.net","foxnews.com","nbcnews.com","cbsnews.com",
"abcnews.go.com","politico.com","axios.com","thehill.com","bloomberg.com","npr.org",
"usatoday.com","newsweek.com","time.com","theatlantic.com","vox.com","msnbc.com",
"telegraph.co.uk","independent.co.uk","thetimes.co.uk","haaretz.com",
"timesofisrael.com","jpost.com","ynetnews.com","foreignpolicy.com","dw.com",
"france24.com","cnbc.com","forbes.com","businessinsider.com","latimes.com"}
ads,_ = adint.load_snap(years=(2024,2025,2026))
byhost = collections.defaultdict(list)
for a in ads:
    for h in a["landing_hosts"]: byhost[h].append(a)
corpus = collections.Counter()
for p in sys.argv[1:]:
    walk_hosts(json.load(open(p)), corpus)
dest = {h:n for h,n in corpus.items() if h not in SOCIAL}
match = {h:n for h,n in dest.items() if h in byhost}
real  = {h:n for h,n in match.items() if h not in NEWS}
print("destination hosts:        %d" % len(dest))
print("matched (raw):            %d  (%.1f%%)" % (len(match),100*len(match)/max(1,len(dest))))
print("  news self-promotion:    %d  <- excluded" % (len(match)-len(real)))
print("  SUBSTANTIVE:            %d  (%.2f%%)\n" % (len(real),100*len(real)/max(1,len(dest))))
for h,n in sorted(real.items(), key=lambda kv:-kv[1]):
    advs = sorted({(a["advertiser"] or a["payer"] or "?") for a in byhost[h]})
    print("  %-34s %3d docs / %3d ads / spend %s / %s imp" % (h,n,len(byhost[h]),
          format(sum(a["spend"] or 0 for a in byhost[h]),","),
          format(sum(a["impressions"] or 0 for a in byhost[h]),",")))
    print("     %s" % ", ".join(a[:40] for a in advs[:4]))
