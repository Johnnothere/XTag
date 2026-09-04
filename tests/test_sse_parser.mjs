import fs from 'fs';
const html = fs.readFileSync('./templates/index.html','utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
// pull out just streamSearch + plainSearch
const start = js.indexOf('function streamSearch');
const end   = js.indexOf('/* What the report pane shows');
const src = js.slice(start,end);

let P=0,F=0;
const chk=(n,c,e='')=>{ c?(P++,console.log(`  PASS  ${n}${e?'  ['+e+']':''}`)):(F++,console.log(`  FAIL  ${n}  ${e}`)); };

globalThis.searchCtl = null;
const mkStream = (chunks) => ({
  ok:true, status:200,
  body:{ getReader(){ let i=0; return { read(){
    return Promise.resolve(i<chunks.length ? {done:false,value:new TextEncoder().encode(chunks[i++])} : {done:true});
  } }; } }
});

const run = async (chunks) => {
  const got=[];
  let fn;
  eval(src + '; fn = streamSearch;');
  globalThis.fetch = async () => mkStream(chunks);
  let done=null;
  await fn('q', d=>got.push(['p1',d]), d=>{got.push(['p2',d]); done=d;});
  return {got,done};
};

const p1 = 'event: phase1\ndata: {"phase":1,"totals":{"mentions":12}}\n\n';
const p2 = 'event: phase2\ndata: {"phase":2,"narratives":[{"label":"n"}]}\n\n';

console.log('\n=== frame parsing ===');
let r = await run([': stream open\n\n', p1, ': keepalive\n\n', p2]);
chk('clean frames', r.got.length===2 && r.got[0][0]==='p1' && r.got[1][0]==='p2', JSON.stringify(r.got.map(x=>x[0])));
chk('keepalive ignored', r.got.length===2);
chk('phase1 data parsed', r.got[0][1].totals.mentions===12);

// the hard case: one frame arrives split across two TCP reads
r = await run([': stream open\n\n', p1.slice(0,20), p1.slice(20), p2]);
chk('frame split mid-payload reassembles', r.got.length===2 && r.got[0][1].totals.mentions===12,
    JSON.stringify(r.got.map(x=>x[0])));

// split exactly on the blank-line boundary
r = await run([p1.slice(0,p1.length-2), p1.slice(p1.length-2)+p2]);
chk('split on the frame delimiter', r.got.length===2, JSON.stringify(r.got.map(x=>x[0])));

// several frames in one read
r = await run([': stream open\n\n'+p1+p2]);
chk('multiple frames in one chunk', r.got.length===2, JSON.stringify(r.got.map(x=>x[0])));

// byte-by-byte — the worst case
r = await run((': stream open\n\n'+p1+p2).split(''));
chk('byte-at-a-time reassembles', r.got.length===2 && r.got[1][1].narratives.length===1,
    JSON.stringify(r.got.map(x=>x[0])));

console.log('\n=== failure modes ===');
try{
  await run([p1, 'event: error\ndata: {"error":"upstream exploded"}\n\n']);
  chk('error event rejects', false);
}catch(e){ chk('error event rejects', /upstream exploded/.test(e.message), e.message); }

try{
  await run([': stream open\n\n', p1]);   // closes with no phase2
  chk('truncated stream rejects', false);
}catch(e){ chk('truncated stream rejects', /closed before/.test(e.message), e.message); }

r = await run([': stream open\n\n', 'event: phase1\ndata: {broken json\n\n', p2]);
chk('malformed JSON skipped, stream survives', r.got.length===1 && r.got[0][0]==='p2');

console.log(`\n${'='.repeat(46)}\n  ${P} passed, ${F} failed\n${'='.repeat(46)}`);
process.exit(F?1:0);
