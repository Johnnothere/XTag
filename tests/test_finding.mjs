import fs from 'fs';
const html = fs.readFileSync('./templates/index.html','utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const start = js.indexOf('function findingClauses');
const end   = js.indexOf('function renderThreatBlock');
const esc = s => s===null||s===undefined||s===''?'':String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
// ESM eval does not leak function declarations to module scope; build them.
const mk = new Function('esc', js.slice(start,end) +
  '; return {findingClauses, findingConfidence, renderFinding, renderLimits};');
const {findingClauses, findingConfidence, renderFinding, renderLimits} = mk(esc);

let P=0,F=0; const chk=(n,c,e='')=>{c?(P++,console.log(`  PASS  ${n}${e?'  ['+e+']':''}`)):(F++,console.log(`  FAIL  ${n}  ${e}`));};
const bad = h => /undefined|NaN|\[object Object\]|null/.test(h.replace(/"[^"]*null[^"]*"/g,''));

const full = {
  totals:{mentions:96}, relevance:{dropped:103, noise_ratio:0.52},
  platforms:{x:{results:[1]},youtube:{results:[1]},google:{results:[1]}},
  threat:{score:37,band:'elevated',confidence:62},
  narratives:[{label:'Founding as disease',key_claim:'Israel\'s founding in 1948 is itself a disease'},{label:'b'}],
  entities:{entities:[{name:'IRGC'},{name:'Press TV'},{name:'DFRLab'}]},
  events:[{kind:'protest'},{kind:'investigation'}],
  coordination:{coordination_score:42, risk:'unbanded',
    caveat:'No matched organic baseline for this query.',
    clusters:[{actor_count:8}], reach:{manufactured_share:0.006}},
  velocity:{acceleration:'accelerating'},
  propagation:{undated:7}, degraded:['sources failed: gdelt'], engine_errors:{},
  narrative_tracking:{semantic:false}
};

console.log('\n=== the full case ===');
let h = renderFinding(full);
chk('renders', h.length>200);
chk('opens with words, not a number', h.indexOf('fd-body') < h.indexOf('fd-score'));
chk('states the evidence base', /96 on-topic documents from 3 sources/.test(h));
chk('says what was removed', /removing 103/.test(h));
chk('quotes the dominant claim', /founding in 1948 is itself a disease/.test(h));
chk('names actors', /IRGC, Press TV, DFRLab/.test(h));
chk('reports offline events', /2 real-world events/.test(h) && /protest activity/.test(h));
chk('coordination present', /8 accounts in 1 cluster/.test(h));
chk('UNBANDED never reads as severity', /not yet knowable/.test(h) && !/high|medium risk/.test(h));
chk('reach verdict is captured', /picked up rather than propped up/.test(h));
chk('velocity stated', /accelerating/.test(h));
chk('confidence is a word first', /fd-conf-w">moderate/.test(h));
chk('score is demoted, not absent', /fd-score-n">37/.test(h));
chk('no undefined/NaN', !bad(h), h.slice(0,120));

console.log('\n=== banded coordination reads differently ===');
h = renderFinding({...full, coordination:{...full.coordination, risk:'high', baseline_ratio:9.1}});
chk('band stated when justified', /\(high against a matched baseline\)/.test(h));
chk('and the hedge is gone', !/not yet knowable/.test(h));

console.log('\n=== self-contained operation ===');
h = renderFinding({...full, coordination:{...full.coordination, reach:{manufactured_share:0.71}}});
chk('says it is talking to itself', /talking to itself/.test(h), h.match(/71%[^<]*/)?.[0]);

console.log('\n=== thin / empty / hostile ===');
chk('empty payload renders nothing', renderFinding({})==='' , JSON.stringify(renderFinding({})));
h = renderFinding({totals:{mentions:3}, platforms:{x:{results:[1]}}, relevance:{}, narratives:[], coordination:{}});
chk('thin corpus still renders a base clause', /3 on-topic documents from 1 source/.test(h));
chk('no narratives -> says so, does not invent', /No distinct narrative clusters/.test(h));
chk('confidence degrades to very low', /very low/.test(h), h.match(/fd-conf-w">[a-z ]+/)?.[0]);
chk('no threat score -> no score block', !/fd-score-n/.test(h));
h = renderFinding({totals:{mentions:5}, platforms:{}, threat:{band:'unknown',score:88}, relevance:{}});
chk('unknown band hides the number', !/fd-score-n/.test(h), 'a band of unknown must not show 88');
chk('hostile strings escaped',
  renderFinding({totals:{mentions:1},platforms:{x:{results:[1]}},relevance:{},
    narratives:[{key_claim:'<img src=x onerror=alert(1)>'}]}).indexOf('<img src=x')===-1);

console.log('\n=== limits ===');
h = renderLimits(full);
chk('lists collection failure', /Collection was incomplete/.test(h));
chk('lists the unbanded caveat', /Coordination has no baseline/.test(h));
chk('lists undated documents', /7 documents carry no usable date/.test(h));
chk('lists lexical clustering', /grouped on wording, not meaning/.test(h));
chk('no limits -> renders nothing', renderLimits({totals:{mentions:90},relevance:{},coordination:{risk:'low'},propagation:{},narrative_tracking:{semantic:true}})==='');
chk('thin corpus is a limit', /corpus is thin/.test(renderLimits({totals:{mentions:4},relevance:{},coordination:{},propagation:{}})));
chk('engine errors surface', /entities stage did not return/.test(renderLimits({totals:{mentions:90},relevance:{},coordination:{},propagation:{},engine_errors:{entities:'timed out'}})));
chk('no undefined in limits', !bad(h));

console.log('\n=== every field absent, one at a time ===');
for (const k of Object.keys(full)) {
  const p = {...full}; delete p[k];
  try { const a=renderFinding(p), b=renderLimits(p);
        if (bad(a)||bad(b)) { chk('missing '+k, false, (a+b).slice(0,90)); } else chk('missing '+k, true); }
  catch(e){ chk('missing '+k, false, e.message); }
}
for (const k of Object.keys(full)) {
  const p = {...full}; p[k]=null;
  try { renderFinding(p); renderLimits(p); chk('null '+k, true); }
  catch(e){ chk('null '+k, false, e.message); }
}

console.log(`\n${'='.repeat(46)}\n  ${P} passed, ${F} failed\n${'='.repeat(46)}`);
process.exit(F?1:0);
