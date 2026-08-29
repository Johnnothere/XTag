# XTag — cross-platform sentiment & narrative intelligence

Editorial "situation room" for public discourse. Search any topic across 15+ platforms,
see real posts with sentiment scoring, narrative clustering, coordination and velocity
signals in one unified feed. Every source activates automatically the moment its API key
env var is set — no code changes needed to bring a platform online.

## Data sources
- **ScrapeBadger**: X/Twitter, TikTok, and Reddit fallback (rich structured data, paid)
- **Reddit official API**: preferred over ScrapeBadger when `REDDIT_CLIENT_ID` /
  `REDDIT_CLIENT_SECRET` are set — free, but personal/research use only per Reddit's terms;
  commercial deployments need Reddit's separate paid developer agreement
- **SerpApi**: Instagram, Facebook, LinkedIn, Pinterest, Threads, Tumblr, Bluesky
- **Direct free APIs**: YouTube, Google News, Hacker News, Mastodon, GDELT, OpenAlex/arXiv
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

## Design
Instrument Serif display + Inter body, editorial intelligence-terminal aesthetic with a
liquid-glass treatment across chrome and analysis cards (frosted blur + saturation,
specular top-edge highlight) — heavier blur reserved for bounded-count surfaces (rail,
topbar, panels, stat/narrative cards); the unbounded mention-card grid uses a cheaper
layered-gradient approximation instead so scroll performance doesn't degrade with a large
result set. Sentiment breakdown bar, filter chips (all/positive/neutral/negative). Light +
dark mode automatic.

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
