/* Regression cover for the law-enforcement-analyst audit fixes.
 *
 * Every check here exists because the live product shipped the opposite. These
 * are all render-path defects — the class the Python suites cannot see, and the
 * class that reached production twice. */
import fs from 'fs';
const html = fs.readFileSync('./templates/index.html','utf8');
const js   = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const esc = s => s===null||s===undefined||s===''?'':String(s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');

let P=0,F=0;
const chk=(n,c,e='')=>{c?(P++,console.log(`  PASS  ${n}${e?'  ['+e+']':''}`))
                        :(F++,console.log(`  FAIL  ${n}  ${e}`));};

// ── U8: the brief reached the screen as raw markdown ────────────────────────
console.log('\n=== U8  brief markdown ===');
{
  const a = js.indexOf('function briefMd(src){');
  const b = js.indexOf('/* renderReport() replaces', a);
  const briefMd = new Function('esc', js.slice(a,b) + '; return briefMd;')(esc);

  const out = briefMd('# INTELLIGENCE BRIEF: HEZBOLLAH\n\n' +
    '**SITUATION**: Israel and *Hezbollah* are engaged in operations.\n\n' +
    '- Drone capability demonstrated\n- Second bullet');
  chk('the leading hash never reaches the screen', !out.includes('# INTELLIGENCE'), out.slice(0,40));
  chk('a heading becomes a heading', out.includes('<div class="bf-h h1">INTELLIGENCE BRIEF: HEZBOLLAH</div>'));
  chk('a run-in label becomes a label', out.includes('<p class="bf-r"><b>SITUATION</b>'));
  chk('italics render', out.includes('<em>Hezbollah</em>'));
  chk('bullets become a list', (out.match(/<li>/g)||[]).length === 2);
  chk('no stray asterisks survive', !/\*/.test(out), out);

  const evil = briefMd('**Note**: <img src=x onerror=alert(1)> and <script>alert(2)</script>');
  chk('markup in the model output is escaped, not rendered',
      !evil.includes('<img') && !evil.includes('<script>'), evil.slice(0,80));
  chk('escaping happens before markdown, so nothing can be smuggled through',
      evil.includes('&lt;img') && evil.includes('&lt;script&gt;'));
}

// ── M2: a date without a year is unusable in a 16-year corpus ───────────────
console.log('\n=== M2  dates carry their year ===');
{
  const a = js.indexOf('function ago(ts){');
  const b = js.indexOf('function durH(h){', a);
  const mk = new Function(js.slice(js.indexOf('function parseTs'), js.indexOf('function parseTs')) +
    'function parseTs(t){var d=new Date(t);return isNaN(d)?null:d;}' +
    js.slice(a,b) + '; return ago;');
  const ago = mk();
  const yr = new Date().getFullYear();
  const old = ago(new Date(Date.UTC(2008, 4, 24)).toISOString());
  chk('an old document shows its year', /200?8|2008/.test(old), old);
  chk('2008 and this year cannot be confused', old !== ago(new Date(Date.UTC(yr,4,24)).toISOString()),
      old + ' vs ' + ago(new Date(Date.UTC(yr,4,24)).toISOString()));
  const recent = ago(new Date(Date.now() - 3*3600*1000).toISOString());
  chk('recent documents stay relative', /^\d+h$/.test(recent), recent);
}

// ── U2 / U3 / U4 / M1 / M5 / M8 / M9: source-level guards ───────────────────
console.log('\n=== render-path guards ===');
const has = (needle, label, why) => chk(label, js.includes(needle), why||needle);

// U2 — "Most relevant" must not collapse into the engagement tiebreak.
has("if(sortMode==='relevance')out.sort(function(a,b){",
    'U2 relevance sort is a composite, not a single term');
chk('U2 recency breaks the relevance tie before engagement does',
    /sortMode==='relevance'[\s\S]{0,400}?tb-ta[\s\S]{0,200}?eng\(b\)-eng\(a\)/.test(js));

// U3/U4 — a badge on every card carries nothing.
chk('U3 an unscored document gets no sentiment pill',
    js.includes("it.sentiment&&it.sentiment!=='unscored'"));
chk("U4 'match' is never printed on a card",
    !js.includes("?'match':'snippet'"), "the match/snippet ternary is still there");

// M1 — the Filter popover.
chk('M1 the toolbar child rule no longer fights the popover over position',
    html.includes('.grid-toolbar>*:not(.filter-panel):not(.sort-panel)'));
chk('M1 the toolbar is a containing block for its popovers',
    /\.grid-toolbar\{[\s\S]*?position:relative;[\s\S]*?\}/.test(html));
has("if(e.target.closest('[data-act]'))return;",
    'M1 the outside-click closer cannot race the action dispatcher');
chk('M1 the corpus can be bounded beyond 30 days',
    js.includes("['90d','Last 90 days']") && js.includes("['1y','Last year']"));
chk('M1 every offered window is also honoured by the filter',
    /'90d':7776000000,\s*'1y':31536000000/.test(js));

// M5 — the analyst's judgement has somewhere to go.
has('function markDoc(url,mark){', 'M5 documents can be marked');
has('function exportCase(fmt){', 'M5 the marked subset can be exported');
chk('M5 a dismissal hides a document without touching a score',
    js.includes("if(!showDismissed&&caseMarks[it.url]===MARK_DROP)return false;"));
chk('M5 marks survive a reload', js.includes("localStorage.setItem(marksKey(lastQ)"));
chk('M5 storage failure never breaks the grid',
    /function saveMarks\(\)\{[\s\S]*?catch\(e\)/.test(js));

// M8 — the narrative cards were buttons that only scrolled.
chk('M8 filterByNarrative actually filters',
    js.includes('narrativeFocus={label:n.label,urls:new Set(n.doc_urls)}'));
chk('M8 a narrative with no resolved documents is not drawn as a control',
    js.includes("var hasDocs=(n.doc_urls&&n.doc_urls.length)?1:0;"));
chk('M8 the focus is wired into the grid filter',
    js.includes("if(narrativeFocus&&!narrativeFocus.urls.has(it.url))return false;"));
chk('M8 the focus can be cleared from the toolbar', js.includes("data-act=\"dropnarr\""));
chk('M8 a new search clears the focus',
    (js.match(/narrativeFocus=null/g)||[]).length >= 4);

// M9 — entity chips were inert.
has('entgrid:function(el){', 'M9 entity chips open their documents');
has('function entDocCount(name){', 'M9 the chip count is the count it will produce');

// M6 — the amplification findings named nothing.
has('function iaExamples(s){', 'M6 amplification signals name their handles');
chk('M6 examples are rendered for every signal', js.includes('iaExamples(s)+'));

// D2/D3/D5/D7 — duplication.
chk('D2 the event feed is silent on a first observation',
    js.includes('if(rest.length&&!firstRun){'));
chk('D3 the purity caveat is dropped only where the corpus card supersedes it',
    js.includes("c.indexOf('off-topic and excluded')<0"));
chk('D5 net sentiment is no longer both a tile and a section',
    !js.includes(">Net Sentiment<") && !js.includes("istat-label\">Net Sentiment"));
chk('D7 the tracked count is not printed three times',
    js.includes('!firstRun&&counts.birth?ntStat('));
chk('D7 number and word are separated', js.includes("'</b> '+"));

// M3 — the language gap.
has('function renderLangGap(data){', 'M3 the language distribution is judged');
chk('M3 a gap is only called where the query was actually issued in that language',
    js.includes('rel.expansion_langs'));

// M10 — what changed, above the fold.
has('function renderChangedSince(data){', 'M10 what changed is a headline');
chk('M10 it renders nothing on a first observation',
    /function renderChangedSince[\s\S]*?if\(maxObs<=1\)return'';/.test(js));
chk('M10 it sits above the brief', js.indexOf('renderChangedSince(data)') < js.indexOf("id=\"briefBody\""));

// U6 — propagation drawn as flow across an archive.
chk('U6 a long spread is not drawn as a chain',
    js.includes('var isArchive=(prop.spread_hours||0)>ARCHIVE_H;'));
chk('U6 the archive case says what it is',
    js.includes('This is archive depth, not a propagation chain.'));

// U9 — a pointer to a file the reader never created.
chk('U9 the dangling dossier pointer is gone',
    !js.includes('is in the downloaded dossier'));

// U5 — the evidence grid was a video wall.
chk('U5 the thumbnail no longer takes a 16:9 block',
    !html.includes('.mc-thumb{width:100%;aspect-ratio:16/9'));

// ── U1 / U7: the scores that could not be argued with ───────────────────────
console.log('\n=== U1 / U7  scoring honesty in the render path ===');
has('function confDrivers(th){', 'U1 every confidence gate is named on screen');
chk('U1 the drivers are attached to the confidence note',
    js.includes('confDrivers(th)+'));
chk('U7 a withheld dimension is never drawn as a bar',
    js.includes("(off?'':'<div class=\"risk-track\">")); 
chk('U7 a withheld dimension is never drawn as a zero',
    js.includes("(off?'\u2014':d.score)"));
chk('U7 the reason for withholding is shown, not just the dash',
    js.includes("offX&&d.reason"));

console.log(`\n  ${P} passed, ${F} failed`);
process.exit(F ? 1 : 0);
