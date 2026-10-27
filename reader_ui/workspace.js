(()=>{
  'use strict';

  const workspace=document.querySelector('.workspace');
  const splitter=document.getElementById('workspace-splitter');
  const desktop=window.matchMedia('(min-width: 921px)');
  const storageKey='reader-workspace-review-width';
  const minReviewWidth=320;
  const minDocumentWidth=300;
  let preferredWidth=null;
  let currentWidth=420;

  function defaultWidth(){
    const total=workspace?.getBoundingClientRect().width||0;
    // Keep the familiar split at ordinary widths; share wide-screen room with review.
    return Math.min(1100,Math.max(420,420+(total-1280)/2));
  }

  function limits(){
    const total=workspace?.getBoundingClientRect().width||0;
    const splitterWidth=splitter?.getBoundingClientRect().width||8;
    return {min:minReviewWidth,max:Math.max(minReviewWidth,total-minDocumentWidth-splitterWidth)};
  }

  function clamp(value){
    const {min,max}=limits();
    return Math.min(max,Math.max(min,Math.round(value)));
  }

  function applyWidth(value,{save=false}={}){
    if(!workspace||!splitter||workspace.hidden)return;
    currentWidth=clamp(value);
    workspace.style.setProperty('--review-width',currentWidth+'px');
    const {min,max}=limits();
    splitter.setAttribute('aria-valuemin',String(min));
    splitter.setAttribute('aria-valuemax',String(max));
    splitter.setAttribute('aria-valuenow',String(currentWidth));
    splitter.setAttribute('aria-valuetext','意见栏宽度 '+currentWidth+' 像素');
    if(save){
      preferredWidth=currentWidth;
      try{localStorage.setItem(storageKey,String(currentWidth));}catch(_error){}
      window.dispatchEvent(new Event('reader:resize'));
    }
  }

  if(workspace&&splitter){
    try{
      const saved=Number(localStorage.getItem(storageKey));
      if(Number.isFinite(saved)&&saved>=minReviewWidth)preferredWidth=saved;
    }catch(_error){}
    applyWidth(preferredWidth??defaultWidth());

    splitter.addEventListener('pointerdown',event=>{
      if(!desktop.matches||event.button!==0)return;
      event.preventDefault();
      splitter.setPointerCapture(event.pointerId);
      splitter.classList.add('dragging');
      document.body.classList.add('resizing-workspace');
    });
    splitter.addEventListener('pointermove',event=>{
      if(!splitter.hasPointerCapture(event.pointerId))return;
      const rect=workspace.getBoundingClientRect();
      applyWidth(rect.right-event.clientX);
    });
    const finishDrag=event=>{
      if(splitter.hasPointerCapture(event.pointerId))splitter.releasePointerCapture(event.pointerId);
      splitter.classList.remove('dragging');
      document.body.classList.remove('resizing-workspace');
      applyWidth(currentWidth,{save:true});
    };
    splitter.addEventListener('pointerup',finishDrag);
    splitter.addEventListener('pointercancel',finishDrag);
    splitter.addEventListener('dblclick',()=>{
      preferredWidth=null;
      try{localStorage.removeItem(storageKey);}catch(_error){}
      applyWidth(defaultWidth());
      window.dispatchEvent(new Event('reader:resize'));
    });
    splitter.addEventListener('keydown',event=>{
      const step=event.shiftKey?50:16;
      let next;
      if(event.key==='ArrowLeft')next=currentWidth+step;
      if(event.key==='ArrowRight')next=currentWidth-step;
      if(next===undefined)return;
      event.preventDefault();
      applyWidth(next,{save:true});
    });
    window.addEventListener('resize',()=>{if(desktop.matches)applyWidth(preferredWidth??defaultWidth());});
  }

  [['change-dialog','change-close'],['delivery-dialog','delivery-close'],['new-review-dialog','new-review-close']].forEach(([dialogId,closeId])=>{
    const dialog=document.getElementById(dialogId);
    const close=document.getElementById(closeId);
    if(!dialog||!close)return;
    close.addEventListener('click',()=>dialog.close());
    dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close();});
  });
})();
