/* Cover for the split, the tab views, and session persistence.
 *
 * The defect this suite exists for: all three tabs rendered the SAME fifteen
 * blocks before appending their own, so "Entities" meant scrolling past the
 * whole narrative report, and Key Entities and the geo breakdown were each
 * drawn under two different tabs. */
import fs from 'fs';
const html = fs.readFileSync('./templates/index.html','utf8');
const js   = html.match(/<script>([\s\S]*?)<\/script>/)[1];

let P=0,F=0;
const chk=(n,c,e='')=>{c?(P++,console.log(`  PASS  ${n}${e?'  ['+e+']':''}`))
                        :(F++,console.log(`  FAIL  ${n}  ${e}`));};

// Slice renderReport into its three branches so each can be inspected alone.
const rr    = js.slice(js.indexOf('function renderReport(data){'));
const rrEnd = rr.indexOf('\nfunction ', rr.indexOf('applyBrief();'));
const body  = rr.slice(0, rrEnd > 0 ? rrEnd : 40000);
const iEnt  = body.indexOf("if(activeMode==='entities')");
const iTl   = body.indexOf("if(activeMode==='timeline')");
const shared    = body.slice(0, iEnt);
const entBranch = body.slice(iEnt, iTl);
const tlBranch  = body.slice(iTl, body.indexOf('// ── NARRATIVE (default)'));
const narrative = body.slice(body.indexOf('// ── NARRATIVE (default)'));

console.log('\n=== tabs are views, not appendices ===');
chk('all three branches exist', iEnt > 0 && iTl > iEnt && narrative.length > 0);

// The heavy analytical blocks must appear under exactly ONE branch.
const owned = {
  renderThreatBlock:      'narrative',
  renderRelevanceBlock:   'narrative',
  renderEventsBlock:      'narrative',
  briefBody:              'narrative',
  renderCoordinationBlock:'entities',
  renderInauthBlock:      'entities',
  renderGeoBlock:         'entities',
  renderEntitiesView:     'entities',
  renderTimelineView:     'timeline',
  renderPropagationBlock: 'timeline',
};
const where = {narrative, entities: entBranch, timeline: tlBranch};
for (const [needle, home] of Object.entries(owned)) {
  const hits = Object.entries(where).filter(([,src]) => src.includes(needle)).map(([k])=>k);
  chk(`${needle} renders under ${home} only`,
      hits.length === 1 && hits[0] === home, hits.join('+') || 'nowhere');
}

chk('the shared prefix no longer carries the whole report',
    !shared.includes('renderThreatBlock') && !shared.includes('renderEventsBlock') &&
    !shared.includes('renderRelevanceBlock'),
    'a heavy block is still rendered on every tab');
chk('the shared prefix keeps the answer',
    shared.includes('renderFinding(data)') && shared.includes('renderLimits(data)'));
chk('the collection gap is shown on every tab, not one in three',
    shared.includes('renderLangGap(data)'));
chk('the language distribution is not repeated outside Entities',
    (body.match(/lang-row/g)||[]).length === 1);

console.log('\n=== duplication inside a tab ===');
chk('Key Entities is not drawn beside the actor list it duplicates',
    !entBranch.includes('Key Entities'));
chk('the actor list is the clickable one',
    js.includes('data-act="entgrid"') && js.includes('class="ent-row'));
chk('the actor count is the count the click produces',
    /var n=entDocCount\(e\.name\);/.test(js));
chk('Velocity is not rendered twice on Timeline',
    !tlBranch.includes('ti-activity"></i>Velocity'));
chk('the standalone events list is not stacked on the timeline',
    !tlBranch.includes('renderEventsBlock'));

console.log('\n=== scroll and tab state ===');
chk('each tab remembers where it was read', js.includes('var tabScroll='));
chk('scroll is restored after the DOM is replaced',
    /restoreTabScroll[\s\S]{0,300}requestAnimationFrame/.test(js));
chk('re-clicking the active tab does not re-render',
    js.includes("if(btn.dataset.mode===activeMode)return;"));
chk('aria-selected tracks the active tab', js.includes("t.setAttribute('aria-selected'"));

console.log('\n=== split ===');
chk('the report gets 60% by default', js.includes('SPLIT_DEFAULT=60'));
chk('the width is a variable, not a hard cap',
    html.includes('.report-panel{width:var(--split,60%)'));
chk('the seam is draggable', html.includes('class="split-h"') && js.includes("h.addEventListener('mousedown',start)"));
chk('the seam answers the keyboard', js.includes("h.addEventListener('keydown'"));
chk('double-click resets to 60/40', js.includes("h.addEventListener('dblclick'"));
chk('the split is clamped to a usable range',
    js.includes('SPLIT_MIN=25') && js.includes('SPLIT_MAX=75'));
chk('the choice is remembered', js.includes("localStorage.setItem(SPLIT_KEY"));
chk('storage failure never breaks the layout',
    /localStorage\.setItem\(SPLIT_KEY[\s\S]{0,40}catch\(e\)/.test(js));
chk('one write per drag, not one per pixel',
    /function stop\(\)\{[\s\S]{0,220}applySplit\(splitPct,true\)/.test(js));
chk('canvas views are told the container changed',
    js.includes('EMAP.resize') && js.includes("new Event('resize')"));
chk('the seam is hidden where there is only one pane',
    html.includes('.split.landing .split-h{display:none;}'));

console.log('\n=== sessions ===');
chk('the rail remembers whether it was open', js.includes("localStorage.setItem(RAIL_KEY"));
chk('the rail opens itself when there is history to find',
    js.includes('setRail(v===null ? loadHist().length>0 : v===\'1\', false)'));
chk('rail init runs after HIST_KEY is assigned, not before',
    js.indexOf('initRail();') > js.indexOf("var HIST_KEY="),
    'initRail would read localStorage under the key `undefined`');
chk('"+" says where the search went', js.includes("Saved \\u201c'+kept+'\\u201d to your searches"));
chk('"+" opens the drawer holding it', /if\(kept\)\{[\s\S]{0,120}setRail\(true,true\)/.test(js));
chk('the saved row is highlighted so it can be found',
    js.includes('function railFlash(id)') && html.includes('.rh-item.flash'));
chk('a collapsed rail still advertises its contents',
    html.includes('id="railCount"') && js.includes("rcn.textContent="));
chk('the restore banner says it cost nothing',
    js.includes('no sources were queried and nothing was spent'));

console.log('\n=== the score is stated once ===');
chk('the threat block does not restate a score the finding already gave',
    js.includes("var solo=!((data.totals||{}).mentions)||!findingClauses(data).length;"));
chk('that check is not a DOM query against unrendered HTML',
    !/var solo=!document\.querySelector/.test(js),
    'the report is built as a string; nothing is in the DOM yet');
chk('the finding strip does not restate its own first sentence',
    !/fd-basis">'\+cf\.docs/.test(js));

console.log(`\n  ${P} passed, ${F} failed`);
process.exit(F ? 1 : 0);
