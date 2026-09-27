import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source=readFileSync(new URL('../src/analysis/kalshi/time_seek.js',import.meta.url),'utf8')+'\n'+readFileSync(new URL('../src/analysis/kalshi/odds_moneyness_browser.js',import.meta.url),'utf8');
const {default:mount}=await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const flush=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};

function setup(){
  delete globalThis.__visualizerTimes;
  delete globalThis.__oddsCamera;
  let now=0,next=0;
  const callbacks=new Map(),writes=[],commits=[],listeners=new Map(),windowListeners=new Map();
  const elements=Object.fromEntries(['#odds-time','#odds-time-value','#odds-play','#odds-pause','#odds-status'].map(key=>[key,{}]));
  const root={querySelector:key=>elements[key]};
  const graph=(key,data)=>({key,data,listeners:new Map(),on(name,fn){this.listeners.set(name,fn);},removeListener(name){this.listeners.delete(name);}});
  const heat=graph('heat',[{z:[[0,null],[.5,.8]]},{}]);
  const surface=graph('surface',[{z:[[0,null],[.5,.8]]},{}]);
  surface.layout={scene:{uirevision:'odds-camera-0'}};surface._fullLayout={scene:{camera:{eye:{x:2}}}};
  globalThis.document={body:{},hidden:false,
    querySelector:selector=>selector.includes('odds_heatmap ')?heat:selector.includes('odds_surface ')?surface:null,
    querySelectorAll:()=>[],addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)};
  globalThis.addEventListener=(name,fn)=>windowListeners.set(name,fn);
  globalThis.removeEventListener=name=>windowListeners.delete(name);
  globalThis.MutationObserver=class{observe(){}disconnect(){}};
  globalThis.performance={now:()=>now};
  globalThis.requestAnimationFrame=fn=>{const id=++next;callbacks.set(id,fn);return id;};
  globalThis.cancelAnimationFrame=id=>callbacks.delete(id);
  globalThis.Plotly={restyle:async(graph,patch,traces)=>{
      writes.push({key:graph.key,patch,traces});
      traces.forEach((trace,index)=>Object.entries(patch).forEach(([field,values])=>{graph.data[trace][field]=structuredClone(values[index]);}));
    },
    relayout:async(graph,patch)=>{writes.push({key:graph.key,patch});if(patch['scene.camera'])surface._fullLayout.scene.camera=patch['scene.camera'];},Plots:{resize:()=>{}}};
  const data={initial_time:.5,price:.6,bid:.4,distance:1,token:'model-1',distances:[0,1,2],
    probability:Array.from({length:101},(_,i)=>i===100?null:.8-i/200),empirical:Array(101).fill(.5),
    n_events:Array(101).fill(25),n_trials:Array(101).fill(50),status:Array(101).fill('supported'),
    slices:Array.from({length:101},(_,i)=>[.8,.8-i/200,null])};
  const component={parentElement:{querySelector:()=>root},data,setStateValue:(name,value)=>commits.push({name,value})};
  const step=milliseconds=>{now+=milliseconds;const pending=[...callbacks.values()];callbacks.clear();pending.forEach(fn=>fn(now));};
  return {elements,root,heat,surface,data,component,writes,commits,callbacks,listeners,windowListeners,step};
}

test('remounts retain an active drag and ignore older seek acknowledgements',()=>{
  const env=setup();let cleanup=mount(env.component);
  const slider=env.elements['#odds-time'];
  slider.value='73';slider.oninput();cleanup();cleanup=mount(env.component);
  assert.equal(slider.value,'73');assert.equal(env.elements['#odds-time-value'].value,'0.73');
  slider.onchange();assert.equal(env.commits.at(-1).value.time,.73);
  slider.value='89';slider.oninput();slider.onchange();cleanup();
  cleanup=mount({...env.component,data:{...env.data,initial_time:.73}});
  assert.equal(slider.value,'89');
  cleanup();cleanup=mount({...env.component,data:{...env.data,initial_time:.89}});
  assert.equal(slider.value,'89');
  cleanup();cleanup=mount({...env.component,data:{...env.data,token:'new-scenario',initial_time:.2}});
  assert.equal(slider.value,'20');cleanup();
});

test('play moves only exact markers while both probability grids remain fixed',async()=>{
  const env=setup(),cleanup=mount(env.component);
  const heatGrid=structuredClone(env.heat.data[0]),surfaceGrid=structuredClone(env.surface.data[0]);
  env.elements['#odds-play'].onclick();env.step(125);await flush();
  assert.equal(env.commits.length,0);
  assert.ok(env.writes.some(write=>write.key==='heat'&&write.patch.customdata?.[0][0][0]===env.data.probability[51]));
  assert.ok(env.writes.some(write=>write.key==='surface'&&write.patch.z?.[0][0]===env.data.probability[51]));
  assert.ok(env.writes.filter(write=>write.traces).every(write=>write.traces[0]===1));
  assert.deepEqual(env.heat.data[0],heatGrid);assert.deepEqual(env.surface.data[0],surfaceGrid);
  assert.deepEqual(env.surface._fullLayout.scene.camera,{eye:{x:2}});
  env.elements['#odds-pause'].onclick();assert.equal(env.commits[0].value.time,.51);
  cleanup();assert.equal(env.callbacks.size,0);assert.equal(env.listeners.size,0);assert.equal(env.windowListeners.size,0);
});

test('both charts send distance/time clicks while holding price fixed',()=>{
  const env=setup(),cleanup=mount(env.component);
  env.heat.listeners.get('plotly_click')({points:[{x:.3,y:Math.log1p(1.5/.1)}]});
  env.surface.listeners.get('plotly_click')({points:[{x:.7,y:Math.log1p(2.5/.1)}]});
  assert.deepEqual(env.commits.map(({value:{distance,...value}})=>value),[
    {token:'model-1',price:.6,bid:.4,time:.3},
    {token:'model-1',price:.6,bid:.4,time:.7}]);
  assert.ok(Math.abs(env.commits[0].value.distance-1.5)<1e-12);
  assert.ok(Math.abs(env.commits[1].value.distance-2.5)<1e-12);
  cleanup();assert.equal(env.heat.listeners.size,0);assert.equal(env.surface.listeners.size,0);
});

test('surface buttons and keyboard seek commit only on pause/seek/end',async()=>{
  const env=setup(),cleanup=mount(env.component);
  const click=action=>env.listeners.get('click')({target:{closest:()=>({dataset:{oddsPlayback:action}})}});
  click('play');env.step(250);await flush();assert.equal(env.commits.length,0);
  click('pause');assert.equal(env.commits[0].value.time,.52);
  const slider=env.elements['#odds-time'];slider.value='99';slider.oninput();await flush();slider.onchange();
  assert.equal(env.commits[1].value.time,.99);
  click('play');env.step(125);await flush();assert.equal(env.commits[2].value.time,1);
  assert.equal(env.callbacks.size,0);cleanup();
});

test('camera survives remounts and reset revision discards the saved camera',async()=>{
  const env=setup(),old=mount(env.component);
  env.surface._fullLayout.scene.camera={eye:{x:4}};
  env.surface.listeners.get('plotly_relayout')({'scene.camera.eye.x':4});
  old();env.surface._fullLayout.scene.camera={eye:{x:1}};
  const next=mount(env.component);await flush();assert.deepEqual(env.surface._fullLayout.scene.camera,{eye:{x:4}});
  next();env.surface.layout.scene.uirevision='odds-camera-1';env.surface._fullLayout.scene.camera={eye:{x:1}};
  const reset=mount(env.component);await flush();assert.deepEqual(env.surface._fullLayout.scene.camera,{eye:{x:1}});reset();
});

test('switching views saves the current browser playback time',async()=>{
  const env=setup(),cleanup=mount(env.component);
  env.elements['#odds-play'].onclick();env.step(500);await flush();
  env.listeners.get('click')({target:{closest:selector=>selector.includes('role="tab"')?{textContent:'3D Surface'}:null}});
  assert.equal(env.commits[0].value.time,.54);assert.equal(env.commits[0].value.view,'3D Surface');
  assert.equal(env.callbacks.size,0);cleanup();
});

test('slow draws coalesce frames and replaced controllers cannot emit stale commits',async()=>{
  const env=setup();const old=mount(env.component);await flush();env.writes.length=0;
  let finish,first=true;
  Plotly.restyle=(graph,patch,traces)=>{
    env.writes.push({key:graph.key,patch,traces});
    if(first){first=false;return new Promise(resolve=>{finish=resolve;});}return Promise.resolve();};
  env.elements['#odds-play'].onclick();
  env.step(125);env.step(125);env.step(125);finish();await flush();
  assert.deepEqual(env.writes.filter(write=>write.key==='heat'&&write.patch.x).map(write=>write.patch.x[0][0]),[.51,.53]);
  const stale=[...env.callbacks.values()][0];
  const next=mount({...env.component,data:{...env.data,token:'model-2',initial_time:.3}});
  stale(1000);await flush();assert.equal(env.commits.length,0);assert.equal(env.elements['#odds-time'].value,'30');
  next();old();
});

test('scenario remount moves dots on the existing charts without refreshing either grid',async()=>{
  const env=setup(),old=mount(env.component);await flush();
  const heat=env.heat,surface=env.surface,heatGrid=structuredClone(heat.data[0]),surfaceGrid=structuredClone(surface.data[0]);
  env.writes.length=0;
  const next=mount({...env.component,data:{...env.data,price:.8,bid:.3,distance:2.234,initial_time:.333,initial_probability:.456,token:'scenario-2'}});
  await flush();
  assert.equal(env.heat,heat);assert.equal(env.surface,surface);
  assert.deepEqual(heat.data[0],heatGrid);assert.deepEqual(surface.data[0],surfaceGrid);
  assert.deepEqual(heat.data[1].x,[.333]);assert.deepEqual(heat.data[1].y,[Math.log1p(2.234/.1)]);
  assert.deepEqual(heat.data[1].customdata,[[.456,2.234]]);assert.deepEqual(surface.data[1].z,[.456]);
  assert.ok(env.writes.every(write=>write.traces?.[0]===1));assert.equal(env.commits.length,0);
  next();old();
});

test('time dragging remains local and chart updates wait for release',async()=>{
  const env=setup(),cleanup=mount(env.component);await flush();env.writes.length=0;
  env.elements['#odds-play'].onclick();const slider=env.elements['#odds-time'];slider.onpointerdown();
  assert.equal(env.callbacks.size,0);
  for(let i=0;i<=93;i++){slider.value=String(i);slider.oninput();}
  await flush();assert.equal(env.writes.length,0);assert.equal(env.commits.length,0);
  assert.equal(env.elements['#odds-time-value'].value,'0.93');
  slider.onchange();slider.onchange();assert.equal(env.commits.length,1);assert.equal(env.commits[0].value.time,.93);
  assert.equal(env.writes.length,0);env.step(16);await flush();
  assert.ok(env.writes.some(write=>write.key==='surface'&&write.patch.x?.[0][0]===.93));
  cleanup();assert.equal(env.callbacks.size,0);
});
