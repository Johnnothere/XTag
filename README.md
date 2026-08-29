# XTag — cross-platform sentiment & narrative intelligence

Editorial "situation room" for public discourse. Search any topic across 15+ platforms,
see real posts with sentiment scoring, narrative clustering, coordination and velocity
signals in one unified feed. Every source activates automatically the moment its API key
env var is set — no code changes needed to bring a platform online.

## Data sources
- **GDELT** — *the backbone*. Broadcast, print and web news from nearly every country in
  100+ languages, machine-translated in near real time, refreshed every 15 minutes. Free,
  no API key, no per-request cost. See the GDELT section below: it is not one source among
  fifteen, it carries the cross-language reach this platform exists for.
- **ScrapeBadger**: X/Twitter, TikTok, and Reddit fallback (rich structured data, paid)
- **Reddit official API**: preferred over ScrapeBadger when `REDDIT_CLIENT_ID` /
  `REDDIT_CLIENT_SECRET` are set — free, but personal/research use only per Reddit's terms;
  commercial deployments need Reddit's separate paid developer agreement
- **SerpApi**: Instagram, Facebook, LinkedIn, Pinterest, Threads, Tumblr, Bluesky
- **Direct free APIs**: YouTube, Google News, Hacker News, Mastodon, OpenAlex/arXiv
- **Telegram**: curated public channels via t.me/s/ preview

Why ScrapeBadger/SerpApi at all instead of "the real API": X's official API is metered
pay-per-use with no free search tier; Instagram's Graph API cannot search arbitrary public
content (only hashtags on your own connected Business account, 30 queries/week); TikTok's
only public API is the Research API, restricted to academic/non-profit non-commercial use.
For X, Instagram, and TikTok there is no free official alternative — ScrapeBadger/SerpApi
is the realistic path, not a stopgap.

## Sentiment
Each result is scored positive / neutral / negative:
- **Claude** (Anthropic API) is the primary engine — set `ANTHROPIC_API_KEY`.
- **Babel Street** is optional enrichment — set `BABELSTREET_API_KEY`; if it works it takes
  priority, otherwise Claude handles it. No hard dependency.
- If neither key is set, sentiment is skipped and everything else still works.

## Environment variables (Railway → Variables)
- `SCRAPEBADGER_KEY` — X/TikTok, Reddit fallback (scrapebadger.com)
- `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` — official Reddit API (reddit.com/prefs/apps,
  create a "script" type app); preferred over ScrapeBadger for Reddit when set
- `SERPAPI_KEY` — Instagram/FB/etc. (serpapi.com)
- `YOUTUBE_API_KEY` — YouTube Data API v3
- `ANTHROPIC_API_KEY` — Claude sentiment scoring
- `BABELSTREET_API_KEY` — optional Babel Street sentiment (uncertain on free trial)
- `TELEGRAM_CHANNELS` — comma-separated public channel names
- `DEBUG_TOKEN` — **required** to use any `/debug/*` route. Unset = those routes return
  401 for everyone. Set this and send it back as the `X-Debug-Token` header.
- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` — persistence (see below); optional
- `MISE_PYTHON_GITHUB_ATTESTATIONS=false` — Railway builder workaround

GDELT needs **no key**. Its tunables (all optional, sane defaults):
- `GDELT_MIN_INTERVAL` (2.0) — seconds between GDELT calls. Lower at your own risk.
- `GDELT_ARTICLE_WINDOWS` (3) — time-slices per search; each buys up to 250 more
  articles and proportionally more block risk. **Lower this first if rate-limited.**
- `GDELT_TIMESPAN_HOURS` (168) — lookback window, 7 days
- `GDELT_BREAKER_COOLDOWN` (180) — backoff after the circuit breaker trips
- `GDELT_ANALYTICS_TTL` (3600) — cache for tone/volume/geography/language series

## Design
Instrument Serif display + Inter body, editorial intelligence-terminal aesthetic with a
liquid-glass treatment across chrome and analysis cards (frosted blur + saturation,
specular top-edge highlight) — heavier blur reserved for bounded-count surfaces (rail,
topbar, panels, stat/narrative cards); the unbounded mention-card grid uses a cheaper
layered-gradient approximation instead so scroll performance doesn't degrade with a large
result set. Sentiment breakdown bar, filter chips (all/positive/neutral/negative). Light +
dark mode automatic.

## GDELT (`gdelt.py`)

GDELT is the primary news source and the basis of the geographic and audience
intelligence layers. It needs no API key and costs nothing per request.

**What it provides**
| mode | what XTag uses it for |
|---|---|
| `artlist` | the articles themselves, across every indexed country and language |
| `timelinetone` | average tone over time → hostile-framing signal, tone trend |
| `timelinevolraw` | hourly article volume → surge detection (peak expressed in σ above baseline) |
| `timelinesourcecountry` | which countries' media carry the narrative → **geographic intelligence** |
| `timelinelang` | which languages it runs in → **audience intelligence** |
| `geo/PointData` | geolocated mentions as mappable coordinates |

**Depth.** The DOC 2.0 API caps `maxrecords` at 250 and has no offset or cursor —
there is no conventional pagination. The only way past 250 is to slice the time
range into consecutive windows and query each. `GDELT_ARTICLE_WINDOWS` (default 3)
controls this, so the default ceiling is ~750 articles over 7 days rather than the
250-over-3-days the old hardcoded implementation was stuck at.

**⚠️ Rate limiting — the thing to know before touching this.**
GDELT rate-limits by IP and does not return 429. It **resets the TCP connection**,
which surfaces as `ConnectionError` / `Connection reset by peer` (curl shows HTTP 000).
During development, roughly 20 requests over a couple of minutes got a container IP
blocked for **more than 15 minutes**, including endpoints that had just worked.

`gdelt.py` is built around that fact:
- every call passes through a process-wide throttle (`GDELT_MIN_INTERVAL`, default 2.0s)
- 3 consecutive connection failures trip a **circuit breaker** that stops calling
  entirely for `GDELT_BREAKER_COOLDOWN` (180s) — continuing to hammer a blocked
  endpoint extends the block rather than recovering from it
- analytical modes are cached for an hour and fetched **sequentially, never in
  parallel** — parallel requests are the fastest way to get blocked
- every function degrades to empty and never raises: a throttled GDELT thins the
  intelligence picture, it never fails a search
- a fully degraded snapshot is deliberately **not** cached, so recovery is immediate
  once a block lifts

Check `gdelt` in `/healthz` — it reports live reachability and current limiter state,
not whether a key is set (there is no key). If it reports rate-limited, lower
`GDELT_ARTICLE_WINDOWS` first.

**Bugs this replaced**, all silent, all in the previous inline implementation:
1. `artlist` returns **no `tone` field** — tone only exists in the timeline modes.
   The old tone→framing mapping never executed once.
2. `language` comes back as a human-readable name (`"Russian"`), not an ISO code, and
   was written straight into `doc["language"]`, so every downstream lookup missed.
3. `sourcecountry` is returned on every article and was discarded entirely. It is now
   the basis of the geographic layer.
4. Timespan was hardcoded to `72H` with no pagination — 250 articles over 3 days, full stop.

GDELT's own language identification now takes precedence over XTag's local detector for
GDELT documents. The local one is script-based and cannot tell Turkish from English (both
Latin) or Ukrainian from Russian (both Cyrillic), so letting it overwrite a correct label
was making the multilingual pipeline *less* accurate.

## Assessment layer (`intel.py`)

Everything else answers *what is being said*. This answers *how much should you care,
and why*. Exposed on `/api/search` as `threat`, `risk`, `audience` and `inauthenticity`.

**Narrative threat score** — composite 0–100 over eight weighted signals: coordination
(20), inauthentic amplification (18), velocity (14), coverage volume (12), geographic
breadth (10), hostile tone (10), source-credibility mix (8), cross-platform propagation (8).
Every score ships with its `factors` array: each factor's own score, weight, contribution
and — when unavailable — the reason. A score you cannot interrogate is a score you cannot
defend.

**Three rules the scoring follows, which matter more than the weights:**

1. **Missing signals are excluded, never scored zero.** A rate-limited GDELT or a dead
   Claude key marks its factors unavailable and the weighted mean renormalises over what
   remains. Scoring absent evidence as zero would drag every total toward "nothing to see
   here" — the most dangerous failure mode a threat score has.
2. **Confidence is reported separately from severity.** A 78 from 6 documents and a 78
   from 900 demand opposite responses. `confidence` and `confidence_band` are their own
   fields, driven by signal coverage, corpus size and analysis-engine errors.
3. **Below 25% signal coverage the band becomes `unknown`, not `low`.** The UI shows a
   dash instead of the number and says "insufficient signal — this is not an all-clear".
   A reader acts on green; a system that cannot see must not render green.

**Risk assessment** — the same signals reweighted into four dimensions that belong to
different owners: `reputational`, `information_integrity`, `operational`,
`physical_security`. Each carries its own score, band, coverage, factors and rationale.

`physical_security` is lexical screening only — density of violent vocabulary, weighted up
when it coincides with coordinated distribution. It flags language for human review. It
does **not** assess intent, capability or credibility, and must not be read as a prediction
of violence. Its `method_note` says so in the API response.

**Inauthenticity detection** — five campaign-shape signals: verbatim reposting by distinct
accounts, bulk-registration handle patterns, single-account flooding (institutional bylines
excluded — a newsroom publishing 30 articles is not flooding), narrative laundering from
state media into unattributed sources, and possible outlet impersonation.

This is **not** a bot classifier and says so in its output. Account age, follower graphs
and posting history are not available from any configured source, so no claim is made about
any individual account being automated. Below 8 documents it returns unavailable rather
than a reassuring zero.

**Audience intelligence** — country distribution, language distribution as a proxy for
target audience, geographic concentration (`concentrated` / `regional` / `diffuse`) and
primary amplifiers. GDELT's breakdown and the corpus's own language mix are reported
separately as well as merged, because when they disagree that disagreement is the signal.

## Persistence (Supabase / Postgres)

Watchlists, alert history, the search cache, and — most importantly — a permanent
time series of every watchlist check are stored in Supabase. This is what makes
"track narratives over time" real: without stored history there is no *over time*.

Set two variables and it activates automatically:

- `SUPABASE_URL` — e.g. `https://<ref>.supabase.co`
- `SUPABASE_SERVICE_KEY` — the **service_role** key (Supabase → Project Settings →
  API Keys). `SUPABASE_SERVICE_ROLE_KEY` also works.

**It must be the service_role key, not the anon/publishable one.** All tables are
RLS deny-all, so the anon key connects fine but silently writes nothing. `/healthz`
performs a real write probe and will tell you explicitly if the wrong key is set —
check `persistence.writable` there after deploying.

If both variables are unset, XTag runs exactly as before on in-memory state. Nothing
breaks; you just don't get history. Persistence is an upgrade, never a dependency —
every DB call fails soft and falls back.

### Tables
| table | purpose |
|---|---|
| `watchlists` | saved queries + alert thresholds; survives redeploys |
| `watchlist_snapshots` | **the time series** — one permanent row per check |
| `alerts` | every triggered alert, indexed for fast history queries |
| `search_cache` | cache that survives restarts, so a redeploy no longer re-pays for upstream API calls |

Schema lives in `supabase/migrations/`.

## Known limitations
- **The GDELT rate limiter is per-process.** It relies on a module-level lock, which
  only serialises correctly because gunicorn runs `--workers 1`. Raising the worker
  count silently multiplies the effective GDELT request rate by the number of workers
  and will get the deployment IP blocked. If workers ever go above 1, the limiter has
  to move to a shared store (Redis or the existing Postgres) first.
- **No threat-score backtesting.** Weights were set from domain reasoning and validated
  against synthetic organic-vs-coordinated scenarios, not against labelled historical
  incidents. They are defensible and transparent, not empirically calibrated. GDELT's
  BigQuery archive is the obvious way to backtest them and has not been done.
- **`physical_security` runs hot on conflict topics** by construction — violent
  vocabulary is the baseline of ordinary war reporting, not an anomaly. The multiplier
  is tuned to keep headroom, but the dimension is a screening aid, not a detector.
- **NotebookLM auth is still in-memory**, so the Knowledge Bank re-syncs from scratch
  after a redeploy. Lower impact than the watchlist problem since it rebuilds itself.
- **Knowledge Bank chat (`/api/kb/chat`) has no UI.** The backend endpoint is fully
  implemented (Claude Q&A over ingested NotebookLM notebooks) but nothing in
  `templates/index.html` calls it — it's dead code from the UI rebuild. Either wire it up
  or remove it.

## Endpoints
- `GET /` — the app
- `GET /api/search?q=Q` — unified JSON incl. sentiment, narratives, entities, velocity,
  coordination, propagation
- `GET /healthz` — lightweight liveness + which sources are configured
- `GET /api/status` — fuller config/source status used by the UI, incl. `persistence`
- `GET /api/history?q=Q[&days=N][&limit=N]` — time series for a query: mentions,
  coordination, sentiment drift, plus per-narrative first/last-seen lifecycle
  (the narrative-emergence signal)
- `GET /api/alerts[?days=N][&limit=N][&unack=1]` — alert history across all watchlists
- `GET /debug/scrapebadger` · `/debug/serpapi` · `/debug/account` · others — all require
  `DEBUG_TOKEN` (see above); never echo full raw keys, only masked last-4
