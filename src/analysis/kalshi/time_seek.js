// Keep drag handlers light: update the thumb/label now, draw after release.
function retainedTime(name, token, serverTime) {
  const states=globalThis.__visualizerTimes || (globalThis.__visualizerTimes=new Map());
  let state=states.get(name);
  if(!state||state.token!==token){
    state={token,serverTime,time:serverTime,pending:null};states.set(name,state);
  }else if(state.pending!=null){
    // A delayed rerender/acknowledgement cannot overwrite a newer browser seek.
    if(serverTime===state.pending){state.serverTime=serverTime;state.pending=null;}
  }else if(!state.dirty&&serverTime!==state.serverTime){
    state.serverTime=serverTime;state.time=serverTime;
  }
  return state;
}

function bindTimeSeek(slider, {onStart, onDrag, onPreview, onCommit, interaction={}}) {
  let frame=null, disposed=false;
  const cancel=()=>{if(frame!=null)cancelAnimationFrame(frame);frame=null;};
  const value=()=>Math.max(0,Math.min(100,Math.round(Number(slider.value))));
  const start=()=>{if(disposed)return;cancel();onStart();};
  slider.onpointerdown=start;
  slider.oninput=()=>{
    if(disposed)return;
    start();interaction.dirty=true;onDrag(value());
  };
  const finish=()=>{
    if(disposed||!interaction.dirty)return;
    cancel();onStart();const index=value();interaction.dirty=false;onDrag(index);
    frame=requestAnimationFrame(()=>{frame=null;if(!disposed)onPreview(index);});
    onCommit();
  };
  slider.onchange=finish;
  slider.onpointercancel=finish;
  return()=>{
    disposed=true;cancel();slider.onpointerdown=null;slider.onpointercancel=null;
    slider.oninput=null;slider.onchange=null;
  };
}
