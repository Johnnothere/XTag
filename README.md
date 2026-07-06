# XTag — cross-platform sentiment & narrative intelligence

Editorial "situation room" for public discourse. Search any topic across 15+ platforms,
see real posts with sentiment scoring in one unified feed.

## Data sources
- **ScrapeBadger**: Reddit, X/Twitter, TikTok (rich structured data)
- **SerpApi**: Instagram, Facebook, LinkedIn, Pinterest, Threads, Tumblr, Bluesky
- **Direct free APIs**: YouTube, Google News, Hacker News, Mastodon
- **Telegram**: curated public channels via t.me/s/ preview

## Sentiment
Each result is scored positive / neutral / negative:
- **Claude** (Anthropic API) is the primary engine — set `ANTHROPIC_API_KEY`.
- **Babel Street** is optional enrichment — set `BABELSTREET_API_KEY`; if it works it takes
  priority, otherwise Claude handles it. No hard dependency.
- If neither key is set, sentiment is skipped and everything else still works.

## Environment variables (Railway → Variables)
- `SCRAPEBADGER_KEY` — Reddit/X/TikTok (scrapebadger.com)
- `SERPAPI_KEY` — Instagram/FB/etc. (serpapi.com)
- `YOUTUBE_API_KEY` — YouTube Data API v3
- `ANTHROPIC_API_KEY` — Claude sentiment scoring
- `BABELSTREET_API_KEY` — optional Babel Street sentiment (uncertain on free trial)
- `TELEGRAM_CHANNELS` — comma-separated public channel names
- `MISE_PYTHON_GITHUB_ATTESTATIONS=false` — Railway builder workaround

## Design
Instrument Serif display + Inter body, editorial intelligence-terminal aesthetic,
sentiment breakdown bar, filter chips (all/positive/neutral/negative), mention-stream cards.
Light + dark mode automatic.

## Endpoints
- `GET /` — the app
- `GET /api/search?q=Q` — unified JSON incl. sentiment
- `GET /healthz` — config status (now includes sentiment_engine)
- `GET /debug/scrapebadger` · `/debug/serpapi` · `/debug/account`
