# XTag backend — fix log

Every fix below is in place, carries an inline comment in the source explaining
the defect it addresses, and was exercised against the real code paths.
`python3 -m py_compile` passes on all five `.py` files and all five modules
import cleanly.

Files touched: `app.py`, `intel.py`, `gdelt.py`, `db.py`, `requirements.txt`,
plus a new `report.html` (also copied to `templates/report.html` — see D1).
`mailer.py`, `Dockerfile` and `Procfile` are unchanged.

---

## A — Correctness

### A1 — physical-threat share had a capped numerator · `intel.py`
`flagged_docs` stops growing at 12 (display cap) but `threat_doc_share` divided
`len(flagged_docs)` by the full corpus, so past 12 hits the dimension **fell** as
more violent documents were found. Added `flagged_total`, an uncapped counter, and
used it for the share; the 12-item list is now purely for display and is named
`FLAGGED_DISPLAY_CAP`. The factor detail reads
`"180 of 200 documents contain threat vocabulary (showing 12)"`, the suffix
appearing only when there is something being withheld.

*Caller-visible:* physical-security scores rise (correctly) on threat-saturated
corpora; new field `risk.dimensions.physical_security.flagged_document_count`.

### A2 — fabricated sentiment readings · `app.py`
`_sentiment_claude` truncated to `texts[:120]` and then padded the tail with
`"neutral"`; `attach_sentiment` could not tell padding from a real reading and
counted it in `counts`, `net_sum` and `scored`. These numbers are persisted as a
time series, so the invention compounded permanently.

* `_sentiment_claude` now pads with `None` (batch size is the named constant
  `CLAUDE_SENTIMENT_BATCH`).
* `attach_sentiment` skips `None`, sets `doc["sentiment"] = "unscored"` on those
  documents, and counts them.
* The aggregate gains `unscored` and `eligible` (`scored + unscored == eligible`).
* Both hardcoded default sentiment blocks carry the new keys.
* `translate_batch` had the same shape and now pads with `None` too — its only
  consumer already skipped falsy values, so behaviour is unchanged there.

*Caller-visible:* `sentiment.scored` gets **smaller** and more honest;
`sentiment.net` is no longer dragged toward 0 by invented neutrals; two new
fields. `intel.score_threat`/`assess_risk` gate the tone factor on
`scored >= 5`, which now means five real readings. Documents that were left with
`sentiment: null` now read `"unscored"`.

### A3 — history froze at the oldest N rows · `db.py`
`snapshots_history` used `order=checked_at.asc&limit=N`, which truncates the
*recent* end: once a query had more than `limit` snapshots, every new snapshot was
invisible forever. Now orders `desc` with the limit and reverses in Python, so
callers still get oldest-first. `None` (DB unreachable) still propagates.

### A4 — LIKE-wildcard injection in the history filter · `db.py`
`query` went straight into a PostgREST `ilike.` filter; `requests.utils.quote` is
a URL encoder, not a LIKE escaper, so `%` read back every row in the table and
`a_c` matched `abc`. Added `_like_literal()` escaping `\`, `%` and `_` with
backslashes (Postgres' default LIKE escape).

**Chose `ilike` over `eq`, deliberately:** the callers (`/api/history` with raw
user input, `_run_one_subscription` with the stored query) depend on
case-insensitivity — rows are written with the casing used at collection time,
so `Hezbollah` and `hezbollah` must be one series. PostgREST additionally
aliases `*` to `%` *before* the SQL escape character is consulted, and there is
no way to escape that; a query containing `*` therefore falls back to `eq.`
(exact, case-sensitive) — narrower than intended, but it can never widen into
"every row in the table". Commented in place.

### A5 — `lstrip("www.")` ate real characters · `app.py`
`_credibility_for_url` used `host.lstrip("www.")`, which strips a character *set*:
`wsj.com` → `sj.com`, `nytimes.com` → `ytimes.com`. Two of the highest-credibility
outlets in `SOURCE_CREDIBILITY` silently scored `unknown` and fed the credibility
threat factor as unattributed sources. Replaced with a `startswith("www.")` prefix
strip, matching `_detect_platform_from_url`.

### A6 — one future-dated post fired an alert and an email · `app.py`
A negative age satisfies `hours <= h` for **every** window at once, so a single
mis-stamped article counted into 1h/6h/24h/48h/72h/7d simultaneously; with
`prior == 0, recent == 1` that met `recent > prior*1.3`, declared "accelerating",
fired a watchlist alert and sent mail.

* Ages below `-FUTURE_SKEW_TOLERANCE_H` (default 2h) are skipped and counted;
  small skew is clamped to 0, because publisher clock drift is normal.
* An "accelerating" verdict now requires `VELOCITY_MIN_DOCS` (default 5)
  documents in the 24h comparison window. Below that the verdict is `"stable"` —
  exactly what the old ratio test already returned at zero volume, so nothing
  regresses; only the unearned "accelerating" is suppressed.

*Caller-visible:* new `velocity.acceleration_basis` (`recent_6h`, `prior_18h`,
`min_docs`, `low_volume`) and `velocity.future_dated_docs`.

### A7 — a banded score from zero documents · `intel.py`
The `coverage < 0.25` guard is satisfied by GDELT-only factors (reach 12 +
geography 10 + tone 10 = 32 of 100 weight), so a real-looking band could be
produced from an empty corpus. Promoted the module's existing 8-document floor to
`MIN_ASSESSABLE_DOCS` (reused by `detect_inauthenticity`) and added it to the
guard: `n_docs < 8` ⇒ `band = "unknown"` with an explicit caveat inserted at the
front of `caveats`. The numeric score is preserved, as specified.

### A8 — full coverage bought "moderate" confidence on no evidence · `intel.py`
`0.55*coverage*100 + 0.45*vol_conf` gave 55 before a single document was read and
72.9 at eight documents ("high"). Chose the **volume-gate** option: the whole
confidence figure is multiplied by `0.35 + 0.65 * min(1, n_docs/100)`, so no
amount of coverage escapes thin evidence, and the numeric confidence and the band
stay consistent with each other. Measured: 0 docs → 6.2 (low); 8 → 16.0 (low);
25 → 24.9 (low); 150 → 62.9 (moderate). Rationale is in the source.

### A9 — coordination counted twice · `intel.py`
`detect_inauthenticity` folded `coordination_score * 0.33` into its own score,
while `score_threat` and `assess_risk` each scored coordination *and*
inauthenticity as separate weighted factors. Symptom: a factor row reading
`score 26.4 / 0 signals; 0 clusters` — a non-zero score whose own evidence string
says there is no evidence. Removed the fold; the score is now purely its own
signals. The `coordination` parameter is kept for call-site compatibility and the
docstring says why it is ignored. Verified no caller depends on the blended value
(the only consumers are the two weighted factors, which score coordination
independently).

*Caller-visible:* `inauthenticity.score` drops for corpora with coordination but
no inauthenticity signals — a corpus with zero signals now scores 0.0.

### A10 — geographic breadth saturated at 40 · `intel.py`
`build_audience` used `len(geo["countries"])`, which GDELT truncates to 40 for
display, making the `full_at=45` curve unreachable — 40 countries and 120 scored
identically. Now uses the `total_countries` field GDELT already returns, falling
back to the list length only when absent. `country_count` reports the real total.

### A11 — a partial corpus reported as complete · `gdelt.py` + `app.py`
`articles()` returned `error=None` whenever *any* window succeeded, so a corpus
assembled from 1 of 3 windows was handed downstream looking whole and every
count, share and density computed from it was quietly wrong. Also added a
truncation signal: a window returning exactly `MAX_RECORDS` almost certainly had
more behind it, and there is no cursor to page past it.

`articles()` now always reports, e.g.
`"1 of 3 time window(s) failed (http 503); 1 window(s) hit the 250-record API cap"`,
carried on a `GdeltError` with `windows_total` / `windows_failed` /
`windows_truncated` attributes. `search_gdelt` surfaces it: `error` is set even
when results exist, plus `partial`, `windows_total`, `windows_failed`,
`windows_truncated` (added only when there is something to report, so a clean
run's shape is unchanged).

### A12 — a timeout permanently downgraded HTTPS → HTTP · `gdelt.py`
One `requests.exceptions.Timeout` called `_record_transport_failure()` and sent
every subsequent request in the process over plaintext. A read timeout says the
server was slow; it says nothing about TLS interception. The downgrade now fires
only on `ConnectionError`/`SSLError`. Also fixed the `-> None` annotation on a
function that returns `bool`, and the connection-error caller now uses that
return value (logging the immediate retry on the new transport).

### A13 — the breaker-abort test never matched · `gdelt.py`
`articles()` aborted the remaining windows by substring-matching `"rate"` or
`"reset"`, but the breaker's own message is
`"backing off after repeated connection failures"` — neither. A tripped breaker
never stopped the loop and every remaining window was spent on a known-dead
endpoint. Added `GdeltError(str)`, a str subclass carrying a machine-readable
`kind` (`breaker | rate_limit | transport | timeout | http | bad_response |
partial | other`) plus the `error_kind()` helper. Subclassing `str` keeps every
existing consumer — logging, f-strings, JSON, truthiness — working unchanged.
The abort now tests `error_kind(err) in ("breaker", "rate_limit")`.

---

## B — Security / cost

### B1 — `/api/kb/chat` was a free proxy to the API key · `app.py`
Client-supplied `history` was forwarded as the `messages` array, so anyone could
post any conversation and have the model answer it on XTag's bill; the
knowledge-bank system prompt constrained nothing because the caller owned the
whole conversation. New `_kb_messages()`: at most `KB_MAX_HISTORY_TURNS` (6)
turns, each truncated to `KB_MAX_TURN_CHARS` (1200), non-`user`/`assistant` roles
dropped, a leading assistant turn dropped, same-role runs collapsed so the array
alternates, and the **final turn is always the server's own**, built from `q`
(itself clamped to `KB_MAX_QUESTION_CHARS`). `max_tokens` is the server constant
`KB_MAX_TOKENS`. Route rate-limited to 15/min.

### B2 — expensive routes were anonymous and unthrottled · `app.py`
One mechanism, no new dependencies:

* `@rate_limit(n, per_seconds)` — fixed-window counter keyed on
  `(route, X-Forwarded-For first hop or remote_addr)`, guarded by a
  `threading.Lock`, table bounded at `RL_MAX_BUCKETS` (20 000) with expiry-then-
  oldest-half eviction. Returns **HTTP 429** with a JSON body and a
  `Retry-After` header. Single-worker gunicorn means one process sees every
  request, so this is a real limit here; it is a cost brake, not an authorisation
  boundary, and the source says so.
* `@require_api_key` — if `XTAG_API_KEY` is set, requires it in an `X-XTag-Key`
  header (`hmac.compare_digest`); **if unset, the route behaves exactly as
  before**, so nothing breaks in dev or for the existing UI.

| Route | Limit | Key gate |
|---|---|---|
| `/api/search` | 6 / 60s | yes |
| `/report` | 4 / 60s | yes |
| `/api/dossier` | 4 / 60s | yes |
| `/api/brief` | 10 / 60s | yes |
| `/api/watchlist/check-all` | 2 / 3600s | yes |
| `/api/subscribe` | 10 / 3600s | yes |
| `/api/watchlist/<id>/check` | 6 / 60s | no |
| `/api/watchlist/<id>` DELETE | 30 / 60s | yes (B5) |
| `/api/subscribe/<id>` DELETE | 30 / 60s | yes (B5) |
| `/api/kb/chat` | 15 / 60s | no |
| `/api/history`, `/api/alerts` | 60 / 60s | no |
| `/api/subscriptions` | 30 / 60s | no |
| `/unsubscribe` | 20 / 60s | no |
| `/healthz` | 120 / 60s | no |

**Beyond the brief, flagged:** `/api/watchlist/<id>/check` is not in the B2 list
but calls `_run_full_search` and costs exactly what `/api/search` costs — leaving
it unthrottled next to a throttled `/api/search` would have defeated the fix, so
it is rate-limited (no key gate, so the existing UI keeps working).

### B3 — `check-all` 500'd on the normal path and discarded finished work
`as_completed(futs, timeout=120)` was unwrapped, and one item's own budget
(`SEARCH_POOL_TIMEOUT`, 240s) already exceeded it — so the *normal* path raised,
Flask returned 500, and every check that had already completed **and been
persisted** was thrown away. Now wrapped: whatever finished is returned, the rest
are reported individually as `"timed out after Ns"`, and the pool is shut down
with `wait=False, cancel_futures=True` so the response is not held hostage.
Deadline is `CHECK_ALL_TIMEOUT` (240s, env-tunable) and the per-call watchlist
count is capped at `CHECK_ALL_MAX` (10, env-tunable).

*Caller-visible:* new response fields `timed_out`, `requested`, `skipped`,
`max_per_request`. Returns 200 with partial results where it used to 500.

### B4 — `/unsubscribe` acted on GET · `app.py`
Corporate mail security (Proofpoint, Defender, Barracuda) and browser link
prefetchers fetch every URL in an inbound message, so recipients who never
clicked were silently unsubscribed the moment the report landed. GET now renders
a confirmation page with a POST form carrying the token in a hidden field; only
POST deactivates. The emailed link itself is unchanged. Existing `html.escape`
handling is kept and extended (`_unsub_page` escapes title, message and token);
added `noindex,nofollow`.

*Caller-visible:* the URL now needs one click to confirm.

### B5 — unauthenticated destructive endpoints · `app.py`
`/api/subscribe/<sub_id>` DELETE had no auth at all; `/api/watchlist/<wl_id>`
DELETE was protected only by a `sha256(query)[:12]` id, which is guessable from
the query. Both are now behind `@require_api_key` (active only when
`XTAG_API_KEY` is set) and rate-limited regardless.

### B6 — `/healthz` billed Anthropic on every hit · `app.py`
`claude_health()` sends a real, billed message; `db.health()` does a write
round-trip; `gdelt.health()` fetches live. Platform probes poll `/healthz` every
few seconds, so the liveness check was a recurring line item and load against
three third parties. `/healthz` is now pure in-process state, no outbound calls.
The deep probe moved behind `?deep=1` with its result cached for
`DEEP_HEALTH_TTL` (120s, ≥ the required 60s) behind its own lock.

*Caller-visible:* `persistence`, `analysis_engine`, `email` and `gdelt` are no
longer in the default `/healthz` body — request `?deep=1` for them. The default
body gains `deep: false` and a `hint`; the deep body gains `deep`, `deep_cached`
and `deep_age_seconds`.

---

## C — Reliability

### C1 — `ANALYSIS_TIMEOUT` was dead config · `app.py`
`_claude_call` hardcoded `timeout=SERPAPI_TIMEOUT` (25s) on the Anthropic POST,
so the future waited 70s for an HTTP call that had already given up at 25 — and
the two largest prompts (narratives at 160 docs, entities at 120) legitimately
need longer. `_claude_call(prompt, max_tokens, timeout=None)` now defaults to the
new `CLAUDE_HTTP_TIMEOUT` (45s, env-tunable); `extract_narratives_v2` and
`extract_entities` pass `ANALYSIS_TIMEOUT`.

### C2 — the pool deadline bounded nothing · `app.py`
`with ThreadPoolExecutor(...)` calls `shutdown(wait=True)` on exit, so
`SEARCH_POOL_TIMEOUT` fired, the handler carefully marked slow sources
"timed out", and then the block blocked on those same sources anyway. Replaced
with explicit `try/finally` + `shutdown(wait=False, cancel_futures=True)`. A
comment notes that already-running tasks still finish in the background — Python
cannot interrupt a thread mid-call — but no longer hold up the response.

**Beyond the strict brief:** the analysis pool (narratives/entities/velocity/
coordination/propagation) had the identical defect making its per-stage budgets
equally toothless, and got the identical fix.

### C3 — notebook store torn down mid-read · `app.py`
`_notebook_store.clear(); _notebook_store.update(synced)` are two mutations on a
dict that `/api/kb/chat` and `search_notebooklm` iterate from request threads: a
request landing between them saw an empty knowledge bank, and one landing during
`update()` raised "dictionary changed size during iteration" and 500'd. Now
builds the replacement first and rebinds the module global — a single atomic
assignment; readers keep the old dict until they are done with it.

### C4 — unlocked, unbounded caches · `app.py`
`_cache` was read and written from request threads *and* the check-all pool with
no lock, and its eviction path sorted the dict while other threads inserted.
`_query_expansion_cache` was unlocked and never evicted anything.

* All `_cache` access goes through `_cache_get` / `_cache_put`, both under
  `_cache_lock`. Every direct use (`_run_full_search` read, warm and write;
  `/api/brief`) was converted.
* Capped by **both** entry count (`CACHE_MAX_ENTRIES`, 200) and a rough
  serialised-size budget (`CACHE_MAX_BYTES`, 48 MB) — 200 full search payloads,
  each carrying every document from every platform, is hundreds of megabytes on
  a single-worker dyno.
* `_query_expansion_cache` gets `_qx_lock` and a `QX_CACHE_MAX` (1000) FIFO cap.

Verified with 8 threads × 400 concurrent put/get: zero exceptions, caps held.

### C5 — the one unpinned dependency · `requirements.txt`
`notebooklm-py[browser]` → `notebooklm-py[browser]==0.8.1` (verified against
PyPI; 0.8.1 is current). Comment explains why: unpinned, two builds of the same
commit could ship different client code, and the `[browser]` extra pulls
Playwright, whose version must match the Chromium binary baked in by
`playwright install chromium` in the Dockerfile — a silent minor bump breaks the
sync thread at runtime, not at build time.

---

## D — The missing template

### D1 — `/report` 500'd after paying for the search · `report.html`
`templates/` contained only `index.html`, so the route rendered a full search and
a Claude brief and *then* threw.

**Two locations, on purpose:** the brief specified
`/root/xtag/backend/report.html`, so it is there — but Flask's loader only looks
in `templates/`, so an identical copy is at
`/root/xtag/backend/templates/report.html`. **Keep them in sync, or move the file
into `templates/` and delete the root copy.**

Print-first intelligence report, standalone, dark-on-white, no external
stylesheets/fonts/scripts/images. Sections: header (classification, query,
generated-at, dossier confidence, threat band); at-a-glance figures; executive
brief; threat assessment with the full factor table (score / weight / share /
contribution / evidence) plus caveats, primary drivers and the E5 input
fingerprint; risk by dimension with per-dimension factor tables, top terms,
flagged documents and method notes; narrative clusters; entities and
relationships; coordination and inauthenticity signals; velocity and propagation
chain; source mix, sentiment (including the A2 `unscored` count), languages and
geography; evidence; confidence and limitations (dossier caveats, GDELT
degradation, engine errors); footer stating every data source and the method
caveats. `@media print` sets page margins, drops link underlines and prevents
breaks inside sections, items, figures and table rows; a mobile breakpoint is
included.

**Escaping:** `|safe` appears nowhere. Every value goes through Jinja
autoescape. The only transform on model text is
`|replace('**','')` on the brief — a plain replace on an already-escaped value,
matching what `mailer.render_report` does. Verified: `<script>` in a scraped
excerpt and in the brief both render inert.

**Degradation:** every lookup is `d.get('x') or {} / []`, so a dossier missing
any section — or `d = {}` entirely — renders without error. Verified against a
full dossier, an empty-search dossier and a bare `{}`.

**Supporting change (`build_dossier`, `app.py`):** the assessment layer was
computed on every search and then dropped here, so neither `/api/dossier` nor the
report could show the interpretive half of the product. `build_dossier` now also
returns `threat`, `risk`, `inauthenticity`, `audience`, `engine_errors` and
`gdelt_degraded`. Purely additive.

---

## E — Smaller

### E1 — the scheduled email analysed dict reprs · `app.py`
`_run_one_subscription` passed `_top_docs()` output (a list of **dicts**) as
`snippets`; `generate_brief` joins them with `str(s)[:220]`, so the model saw
220 characters of `{'id': '9f3c…', 'platform': 'x', …}` per post and never a word
of text. It then passed `generate_brief`'s **dict** where `mailer.render_report`
expects a string, printing `{'brief': '...'}` in the email. Both fixed: text is
extracted the way `/api/brief` and `/api/dossier` already do, and
`br.get("brief")` is passed on.

### E2 — engine crashes cost no confidence · `intel.py`
`assess()` recorded stage crashes in `out["errors"]`, but `score_threat` reads
`payload["engine_errors"]` — a key `assess()` never wrote. Wired together:
`assess()` builds a shallow copy of the payload with its own errors merged in
under `assess_<stage>` keys (the caller's payload is not mutated) and scores from
that. Measured: a crashed audience stage now drops confidence 48.4 → 35.4 and
adds the caveat.

### E3 — "0 signals" next to "not assessed" · `intel.py`
`assess_risk` passed `detail` unconditionally, so an unavailable factor rendered
a measurement that was never taken. `detail` is now `None` whenever the factor is
unavailable (inauthenticity, coordination, volume, velocity ×2, platforms).

### E4 — displayed weight was not the weight used · `intel.py`
`Factor.as_dict` emitted the configured `weight` while `_composite` renormalises
over available factors — a factor configured at 20 could be carrying 50% of the
score. Now emits **both**: `weight` (design intent) and `contribution_pct` (share
actually carried in this assessment). Additive; the report table shows both.

### E5 — no input fingerprint · `intel.py` (+ `gdelt.py`)
Added `inputs` — `query`, `n_docs`, `timespan_hours`, `gdelt_cached` — to both the
`threat` block (so it reaches `payload["threat"]` and the report) and the
`assess()` return. `gdelt.snapshot()` now echoes `timespan_hours` so consumers do
not have to guess the module's defaults; `cached: True` was already set.

### E6 — shallow copy of a cached snapshot · `gdelt.py`
`{**cached, "cached": True}` left every nested list and dict pointing at the cache
entry, so a caller that sorted, truncated or annotated
`snapshot["geography"]["countries"]` corrupted it for the next hour. Now
`copy.deepcopy`.

---

## Environment variables introduced

All optional; every one has a default that preserves current behaviour.

| Variable | Default | Purpose |
|---|---|---|
| `XTAG_API_KEY` | *(unset)* | B2/B5 shared-secret gate. **Unset = open, as today.** |
| `RL_MAX_BUCKETS` | `20000` | B2 rate-limiter table bound |
| `CLAUDE_HTTP_TIMEOUT` | `45` | C1 default Anthropic HTTP budget |
| `CACHE_MAX_ENTRIES` | `200` | C4 search-cache entry cap |
| `CACHE_MAX_BYTES` | `50331648` | C4 search-cache byte budget |
| `QUERY_EXPANSION_CACHE_MAX` | `1000` | C4 expansion-cache cap |
| `CHECK_ALL_TIMEOUT` | `240` | B3 check-all deadline |
| `CHECK_ALL_MAX` | `10` | B3 watchlists per check-all call |
| `DEEP_HEALTH_TTL` | `120` | B6 deep-probe cache |
| `FUTURE_SKEW_TOLERANCE_H` | `2` | A6 tolerated clock skew |
| `VELOCITY_MIN_DOCS` | `5` | A6 floor for an "accelerating" verdict |
| `KB_MAX_HISTORY_TURNS` | `6` | B1 |
| `KB_MAX_TURN_CHARS` | `1200` | B1 |
| `KB_MAX_QUESTION_CHARS` | `1200` | B1 |
| `KB_MAX_TOKENS` | `600` | B1 (was already the hardcoded value) |
