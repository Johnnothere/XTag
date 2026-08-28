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
- `MISE_PYTHON_GITHUB_ATTESTATIONS=false` — Railway builder workaround

## Design
Instrument Serif display + Inter body, editorial intelligence-terminal aesthetic with a
liquid-glass treatment across chrome and analysis cards (frosted blur + saturation,
specular top-edge highlight) — heavier blur reserved for bounded-count surfaces (rail,
topbar, panels, stat/narrative cards); the unbounded mention-card grid uses a cheaper
layered-gradient approximation instead so scroll performance doesn't degrade with a large
result set. Sentiment breakdown bar, filter chips (all/positive/neutral/negative). Light +
dark mode automatic.

## Known limitations
- **No persistent storage.** The search cache, watchlists, and NotebookLM auth all live in
  process memory only. Every Railway redeploy wipes watchlists entirely, and running more
  than one gunicorn worker means requests can silently hit a worker that never saw a given
  watchlist. `--workers 1` (see Procfile/Dockerfile) fixes the second problem; the first
  means "continuous monitoring" and "narrative emergence over time" — both stated project
  goals — don't yet survive a restart. A real datastore (Postgres/Supabase, or a Railway
  volume + SQLite) is the natural next step and isn't in place yet.
- **Knowledge Bank chat (`/api/kb/chat`) has no UI.** The backend endpoint is fully
  implemented (Claude Q&A over ingested NotebookLM notebooks) but nothing in
  `templates/index.html` calls it — it's dead code from the UI rebuild. Either wire it up
  or remove it.

## Endpoints
- `GET /` — the app
- `GET /api/search?q=Q` — unified JSON incl. sentiment, narratives, entities, velocity,
  coordination, propagation
- `GET /healthz` — lightweight liveness + which sources are configured
- `GET /api/status` — fuller config/source status used by the UI
- `GET /debug/scrapebadger` · `/debug/serpapi` · `/debug/account` · others — all require
  `DEBUG_TOKEN` (see above); never echo full raw keys, only masked last-4
