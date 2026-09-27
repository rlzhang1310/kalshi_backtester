// Run with node --test tests/fill_playback.test.mjs. No browser or network used.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source = readFileSync(new URL('../src/analysis/kalshi/time_seek.js', import.meta.url), 'utf8') + '\n' + readFileSync(new URL('../src/analysis/kalshi/fill_playback.js', import.meta.url), 'utf8');
const {default:mount} = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const flush = async () => { for (let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve)); };

function setup() {
  delete globalThis.__visualizerTimes;
  let now=0, next=0;
  const callbacks=new Map(), writes=[], commits=[];
  const elements=Object.fromEntries(['#fill-time','#fill-time-value','#fill-play','#fill-pause','#fill-playback-status'].map(key=>[key,{}]));
  const root={querySelector:key=>elements[key]};
  const graphs={
    no_clv_heatmap:{key:'heat',data:[{},{}]},
    no_clv_time_surface:{key:'surface',data:[{},{},{}],layout:{scene:{camera:{eye:{x:2}}}}},
    no_clv_time_series:{key:'series',data:[{},{}]},
  };
  const listeners=new Map();
  globalThis.document={hidden:false,
    querySelector:selector=>Object.entries(graphs).find(([key])=>selector.includes(`st-key-${key} `))?.[1] || null,
    querySelectorAll:()=>[], addEventListener:(key,fn)=>listeners.set(key,fn), removeEventListener:(key)=>listeners.delete(key),
  };
  globalThis.performance={now:()=>now};
  globalThis.requestAnimationFrame=callback=>{ const id=++next;callbacks.set(id,callback);return id; };
  globalThis.cancelAnimationFrame=id=>callbacks.delete(id);
  globalThis.Plotly={
    restyle:async (graph,patch,traces)=>writes.push({key:graph.key,patch,traces}),
    relayout:async (graph,patch)=>writes.push({key:graph.key,patch}),
  };
  const data={initial_time:.5,price:.6,bid:.2,token:'model-1',family:'KXTEST',ticker:null,minimum:2,
    prices:[25,60], bids:[0,20],
    frames:Array.from({length:101},(_,i)=>({z:[[.75,.4],[null,i/100]],events:[[2,2],[0,2]],observations:[[4,4],[0,4]]})),
    selected:Array.from({length:101},(_,i)=>i===100?null:.4+i/200),
    raw:Array(101).fill(.5),events:Array(101).fill(2),observations:Array(101).fill(4),status:Array(101).fill('supported'),
  };
  const component={parentElement:{querySelector:()=>root},data,setStateValue:(name,value)=>commits.push({name,value})};
  const step=milliseconds=>{now+=milliseconds;const pending=[...callbacks.values()];callbacks.clear();pending.forEach(callback=>callback(now));};
  return {elements,root,graphs,writes,commits,component,data,callbacks,listeners,step};
}

test('playback updates linked exact markers without Python reruns or camera resets',async()=>{
  const env=setup();const cleanup=mount(env.component);
  env.elements['#fill-play'].onclick();env.step(125);await flush();
  assert.equal(env.elements['#fill-time'].value,'51');
  assert.equal(env.commits.length,0);
  assert.ok(env.writes.some(write=>write.key==='surface'&&write.patch.x?.[0][0]===.51));
  assert.ok(env.writes.some(write=>write.key==='series'&&write.patch.y?.[0][0]===env.data.selected[51]));
  assert.ok(env.writes.some(write=>write.key==='heat'&&write.patch.z?.[0][1][0]===null));
  assert.deepEqual(env.graphs.no_clv_time_surface.layout.scene.camera,{eye:{x:2}});
  env.elements['#fill-pause'].onclick();
  assert.equal(env.commits.length,1);assert.equal(env.commits[0].value.time,.51);
  assert.equal(env.callbacks.size,0);cleanup();assert.equal(env.listeners.size,0);
});

test('keyboard/manual seeks commit once and completion stops at one',async()=>{
  const env=setup();const cleanup=mount(env.component);
  const slider=env.elements['#fill-time'];slider.value='99';slider.oninput();await flush();
  assert.equal(env.commits.length,0);slider.onchange();assert.equal(env.commits[0].value.time,.99);
  env.elements['#fill-play'].onclick();env.step(125);await flush();
  assert.equal(slider.value,'100');assert.equal(env.commits[1].value.time,1);
  assert.equal(env.callbacks.size,0);assert.equal(env.elements['#fill-play'].disabled,false);
  cleanup();
});

test('surface controls play an exact moving slice and pause commits time',async()=>{
  const env=setup();
  env.graphs.no_clv_time_surface.data.splice(2,0,{name:'Current time slice'});
  env.data.slices=Array.from({length:101},(_,i)=>[.4,i===100?null:i/100]);
  const cleanup=mount(env.component);
  const click=action=>env.listeners.get('click')({target:{closest:()=>({dataset:{fillPlayback:action}})}});
  click('play');env.step(125);await flush();
  const slice=env.writes.find(write=>write.key==='surface'&&write.traces?.[0]===2);
  assert.deepEqual(slice.patch.x,[[.51,.51]]);
  assert.deepEqual(slice.patch.y,[env.data.bids]);
  assert.deepEqual(slice.patch.z,[env.data.slices[51]]);
  click('pause');assert.equal(env.commits[0].value.time,.51);
  cleanup();assert.equal(env.listeners.size,0);
});

test('slow draws coalesce pending frames and cleanup cancels stale frames',async()=>{
  const env=setup();let finish;let first=true;
  Plotly.restyle=(graph,patch,traces)=>{
    env.writes.push({key:graph.key,patch,traces});
    if(first){first=false;return new Promise(resolve=>{finish=resolve;});}
    return Promise.resolve();
  };
  const cleanup=mount(env.component);env.elements['#fill-play'].onclick();
  env.step(125);env.step(125);env.step(125);
  finish();await flush();
  const frames=env.writes.filter(write=>write.key==='surface'&&write.patch.x).map(write=>write.patch.x[0][0]);
  assert.deepEqual(frames,[.51,.53]);
  env.step(125);cleanup();const count=env.writes.length;env.step(10000);await flush();
  assert.equal(env.writes.length,count);assert.equal(env.commits.length,0);
});

test('remount disposes the previous controller and rejects its pending callbacks',async()=>{
  const env=setup();const old=mount(env.component);env.elements['#fill-play'].onclick();
  const stale=[...env.callbacks.values()][0];
  const latest=mount({...env.component,data:{...env.data,initial_time:.3,token:'model-2'}});
  stale(1000);await flush();assert.equal(env.writes.length,0);assert.equal(env.commits.length,0);
  assert.equal(env.elements['#fill-time'].value,'30');latest();old();
});

test('dragging pauses playback and updates only the thumb/label until release',async()=>{
  const env=setup(),cleanup=mount(env.component);
  env.elements['#fill-play'].onclick();
  const slider=env.elements['#fill-time'];slider.onpointerdown();
  assert.equal(env.callbacks.size,0);
  for(let i=0;i<=87;i++){slider.value=String(i);slider.oninput();}
  await flush();assert.equal(env.writes.length,0);assert.equal(env.commits.length,0);
  assert.equal(env.elements['#fill-time-value'].value,'0.87');
  slider.onchange();slider.onchange();
  assert.equal(env.commits.length,1);assert.equal(env.commits[0].value.time,.87);
  assert.equal(env.writes.length,0);
  env.step(16);await flush();assert.ok(env.writes.some(write=>write.key==='surface'&&write.patch.x?.[0][0]===.87));
  cleanup();assert.equal(env.callbacks.size,0);
});

test('a new drag cancels the previous release preview and disposal cancels pending work',async()=>{
  const env=setup(),cleanup=mount(env.component),slider=env.elements['#fill-time'];
  slider.value='20';slider.oninput();slider.onchange();
  slider.onpointerdown();slider.value='30';slider.oninput();
  env.step(16);await flush();assert.equal(env.writes.length,0);
  slider.onpointercancel();assert.equal(env.commits.at(-1).value.time,.3);
  cleanup();env.step(16);await flush();assert.equal(env.writes.length,0);
});

test('stale server remounts and older seek acknowledgements do not rewind the thumb',()=>{
  const env=setup();let cleanup=mount(env.component);
  const slider=env.elements['#fill-time'];
  slider.value='72';slider.oninput();cleanup();cleanup=mount(env.component);
  assert.equal(slider.value,'72');
  slider.onchange();assert.equal(env.commits.at(-1).value.time,.72);
  slider.value='86';slider.oninput();slider.onchange();cleanup();
  cleanup=mount({...env.component,data:{...env.data,initial_time:.72}});
  assert.equal(slider.value,'86');
  cleanup();cleanup=mount({...env.component,data:{...env.data,initial_time:.86}});
  assert.equal(slider.value,'86');
  cleanup();cleanup=mount({...env.component,data:{...env.data,initial_time:.31}});
  assert.equal(slider.value,'31');cleanup();
});
