// Browser-owned animation; Python is notified only on seek/pause/end/click.
export default function(component) {
  const {parentElement, data, setStateValue} = component;
  const root = parentElement.querySelector('.odds-playback');
  root.__playback?.dispose();
  const slider = root.querySelector('#odds-time'), output = root.querySelector('#odds-time-value');
  const play = root.querySelector('#odds-play'), pause = root.querySelector('#odds-pause'), status = root.querySelector('#odds-status');
  let disposed=false, playing=false, request=null, started=0, startTime=0;
  const timeState=retainedTime('odds',data.token,data.initial_time);
  let current=Math.round(timeState.time*100), pending=null, busy=false;
  let currentTime=timeState.time, currentProbability=currentTime===data.initial_time&&data.initial_probability!==undefined?data.initial_probability:data.probability[current];
  let heat=null, surface=null, restoring=false;
  const cameraState=globalThis.__oddsCamera || (globalThis.__oddsCamera={camera:null,revision:null});
  const graph=key=>document.querySelector(`.st-key-${key} .js-plotly-plot`);
  const format=value=>value==null?'Unsupported':`${(value*100).toFixed(2)}%`;
  const sync=(distance=null,time=currentTime,view=null)=>{
    if(disposed)return;
    timeState.time=time;timeState.pending=time;
    setStateValue('selection',{token:data.token,price:data.price,bid:data.bid,time,...(distance==null?{}:{distance}),...(view==null?{}:{view})});
  };
  const buttons=()=>{
    play.disabled=playing;pause.disabled=!playing;
    document.querySelectorAll('[data-odds-playback]').forEach(button=>{
      button.disabled=button.dataset.oddsPlayback==='play'?playing:!playing;
    });
  };
  const stop=commit=>{
    playing=false;if(request!=null)cancelAnimationFrame(request);request=null;buttons();
    if(commit)sync();
  };
  const updateReadouts=index=>{
    const labels=[format(data.probability[index]),data.empirical[index]==null?'Unavailable':format(data.empirical[index]),data.status[index],(index/100).toFixed(2)];
    document.querySelectorAll('.st-key-odds_readouts [data-testid="stMetricValue"]').forEach((value,i)=>{
      if(i<labels.length)(value.querySelector('div')||value).textContent=labels[i];
    });
    const coverage=document.querySelector('.st-key-odds_coverage p');
    if(coverage)coverage.textContent=`Local coverage: ${data.n_events[index]} games · ${data.n_trials[index]} state-bid trials. Blank regions are masked; observed 0% remains visible.`;
  };
  const apply=async selection=>{
    if(disposed||!globalThis.Plotly)return;
    const {time,probability,index}=selection,jobs=[];
    const axisDistance=Math.sign(data.distance)*Math.log1p(Math.abs(data.distance)/.1);
    if(heat?.data)jobs.push(
      Plotly.restyle(heat,{x:[[time]],y:[[axisDistance]],customdata:[[[probability,data.distance]]]},[heat.data.length-1])
    );
    if(surface?.data)jobs.push(
      Plotly.restyle(surface,{x:[[time]],y:[[axisDistance]],z:[[probability]],customdata:[[[probability,data.distance]]]},[surface.data.length-1])
    );
    if(index!=null)updateReadouts(index);await Promise.all(jobs);
  };
  const drain=async()=>{
    if(busy||disposed)return;busy=true;
    try{while(pending!=null&&!disposed){const selection=pending;pending=null;await apply(selection);}}
    catch(error){stop(false);status.textContent='Playback stopped. Select a time or reload the view.';}
    finally{busy=false;}
  };
  const select=(index,draw=true)=>{
    current=Math.max(0,Math.min(100,index));slider.value=String(current);output.value=(current/100).toFixed(2);
    currentTime=current/100;currentProbability=data.probability[current];
    timeState.time=currentTime;
    if(draw){pending={time:currentTime,probability:currentProbability,index:current};void drain();}
  };
  const tick=now=>{
    if(!playing||disposed)return;
    const index=Math.min(100,Math.round(startTime+(now-started)/125));
    if(index!==current)select(index);
    if(current===100){stop(true);status.textContent='Completed';return;}
    request=requestAnimationFrame(tick);
  };
  play.onclick=()=>{
    if(playing||disposed)return;if(current===100)select(0);
    playing=true;started=performance.now();startTime=current;buttons();status.textContent='Playing · no page reloads';
    request=requestAnimationFrame(tick);
  };
  pause.onclick=()=>{stop(true);status.textContent='Paused';};
  const disposeSeek=bindTimeSeek(slider,{
    interaction:timeState,
    onStart:()=>{stop(false);pending=null;status.textContent='Paused';},
    onDrag:index=>{select(index,false);status.textContent='Selecting time…';},
    onPreview:index=>select(index),onCommit:()=>{status.textContent='Paused';sync();},
  });
  const onControl=event=>{
    const tab=event.target.closest?.('.st-key-odds_view [role="tab"]');
    const view=tab?.textContent?.trim();
    if(['Heatmap','3D Surface'].includes(view)&&!disposed){stop(false);sync(null,currentTime,view);return;}
    const button=event.target.closest?.('[data-odds-playback]');if(!button||disposed)return;
    if(button.dataset.oddsPlayback==='play')play.onclick();
    if(button.dataset.oddsPlayback==='pause')pause.onclick();
  };
  const onVisibility=()=>{if(document.hidden&&playing)stop(true);};
  const onPoint=event=>{
    const point=event.points?.[0];if(disposed||!Number.isFinite(point?.x)||!Number.isFinite(point?.y))return;
    if(point.x<0||point.x>1)return;
    const distance=.1*Math.sign(point.y)*Math.expm1(Math.abs(point.y));
    if(!Number.isFinite(distance))return;
    stop(false);sync(distance,point.x);
  };
  const rememberCamera=event=>{
    if(restoring||!surface?._fullLayout?.scene||!Object.keys(event).some(key=>key.startsWith('scene.camera')))return;
    cameraState.camera=JSON.parse(JSON.stringify(surface._fullLayout.scene.camera));cameraState.revision=surface.layout.scene.uirevision;
  };
  const restoreCamera=()=>{
    if(disposed||restoring||!surface?._fullLayout?.scene||!globalThis.Plotly)return;
    const revision=surface.layout.scene.uirevision;
    if(cameraState.revision!==revision){cameraState.revision=revision;cameraState.camera=null;}
    if(cameraState.camera&&JSON.stringify(cameraState.camera)!==JSON.stringify(surface._fullLayout.scene.camera)){
      restoring=true;
      Promise.resolve(Plotly.relayout(surface,{'scene.camera':JSON.parse(JSON.stringify(cameraState.camera))})).finally(()=>{restoring=false;});
    }
  };
  const detach=()=>{
    heat?.removeListener?.('plotly_click',onPoint);surface?.removeListener?.('plotly_click',onPoint);
    surface?.removeListener?.('plotly_relayout',rememberCamera);surface?.removeListener?.('plotly_afterplot',restoreCamera);
  };
  const scan=()=>{
    if(disposed)return;
    const nextHeat=graph('odds_heatmap'),nextSurface=graph('odds_surface');
    if(nextHeat===heat&&nextSurface===surface)return;
    detach();heat=nextHeat;surface=nextSurface;
    heat?.on?.('plotly_click',onPoint);surface?.on?.('plotly_click',onPoint);
    surface?.on?.('plotly_relayout',rememberCamera);surface?.on?.('plotly_afterplot',restoreCamera);restoreCamera();
    pending={time:currentTime,probability:currentProbability,index:null};void drain();
  };
  const resize=()=>[heat,surface].forEach(chart=>{if(chart&&globalThis.Plotly?.Plots)Plotly.Plots.resize(chart);});
  const observer=new MutationObserver(scan);observer.observe(document.body,{childList:true,subtree:true});
  document.addEventListener('click',onControl,true);document.addEventListener('visibilitychange',onVisibility);globalThis.addEventListener('resize',resize);
  const state={dispose:()=>{
    if(disposed)return;
    disposed=true;pending=null;stop(false);observer.disconnect();detach();
    disposeSeek();
    document.removeEventListener('click',onControl,true);document.removeEventListener('visibilitychange',onVisibility);globalThis.removeEventListener('resize',resize);
    play.onclick=null;pause.onclick=null;slider.oninput=null;slider.onchange=null;
  }};
  root.__playback=state;slider.value=String(current);output.value=currentTime.toFixed(2);buttons();scan();
  return()=>{state.dispose();if(root.__playback===state)delete root.__playback;};
}
