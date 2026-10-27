(()=>{
  'use strict';
  const el=(tag,cls='',text)=>{const n=document.createElement(tag);n.className=cls;if(text!==undefined)n.textContent=text;return n;};
  const btn=(text,action,cls='review-job-secondary')=>{const n=el('button',cls,text);n.type='button';n.addEventListener('click',action);return n;};
  let current=null,dialog=null,opening=false,configChanged=null;
  const attempts=new Map();
  const endpoint=id=>'/api/reports/'+encodeURIComponent(id)+'/rereview';
  async function request(path,payload){const response=await fetch(path,payload===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const data=await response.json();if(!response.ok){const error=new Error(data.error||'全文复审未能启动。');error.status=response.status;throw error;}return data;}
  // The model writes P017 / F01 in its round notes; show clause numbers and the list position instead.
  function notesReadable(bundle){
    const paragraphs=bundle.paragraphs||[];
    // Frozen so ReaderText builds its label tables once for all the notes, not once per line.
    const locations=Object.freeze(Object.fromEntries(paragraphs.map(p=>[p.id,(p.location||p.display_text||p.text||'对应原文').replaceAll(' / ',' · ')])));
    const inline_locations=Object.freeze(Object.fromEntries(paragraphs.map(p=>[p.id,window.ReaderText?.shortReference(p)||locations[p.id]])));
    const finding_labels=Object.freeze(Object.fromEntries((bundle.report?.findings||[]).map((f,index)=>[f.id,'意见'+(index+1)])));
    return text=>window.ReaderText?.humanize(text,{locations,inline_locations,paragraphs,finding_labels},{plain:true,shortLocations:true,hideUnknown:true})||text;
  }
  function load(bundle){
    current=bundle;const trigger=document.getElementById('rereview');if(trigger)trigger.disabled=false;
    const parent=bundle.parent_report_id||bundle.report?.metadata?.rereview?.parent_report_id||bundle.report?.parent_report_id||bundle.report?.metadata?.parent_report_id||bundle.report?.metadata?.trace?.parent_report_id;
    const previous=document.getElementById('previous-round');if(previous){previous.hidden=!parent;previous.textContent=bundle.parent_report_deleted?'前一轮已删除':'← 回到前一轮';if(parent&&!bundle.parent_report_deleted)previous.href='/?report='+encodeURIComponent(parent);else previous.removeAttribute('href');}
    const next=document.getElementById('next-round');if(next){next.hidden=!bundle.next_report_id;if(bundle.next_report_id)next.href='/?report='+encodeURIComponent(bundle.next_report_id);else next.removeAttribute('href');}
    const round=Number(bundle.report?.metadata?.rereview?.round_number||bundle.round_number||1),label=document.getElementById('review-round');
    if(label){label.textContent='第'+round+'轮'+(parent?'复审':'初审');label.hidden=false;}
    const notes=document.getElementById('rereview-notes');
    if(notes){notes.replaceChildren();const limitations=(bundle.report?.limitations||[]).filter(text=>typeof text==='string'&&text.trim());notes.hidden=!parent||!limitations.length;if(!notes.hidden){const detail=el('details','contract-facts');detail.append(el('summary','','本轮复审说明'));const readable=notesReadable(bundle);for(const raw of limitations){const text=readable(raw),part=el('div','rereview-note');if(window.ChecklistsUI?.renderContent)window.ChecklistsUI.renderContent(part,text);else part.textContent=text;detail.append(part);}notes.append(detail);}}
  }
  async function open(){
    if(!current||opening)return;if(dialog?.open){dialog.focus();return;}opening=true;const bundle=current;
    dialog=el('dialog','work-dialog review-job-dialog rereview-dialog');const head=el('header','dialog-head'),body=el('div','dialog-body');head.append(el('h2','','复审全文'),btn('×',()=>dialog.close(),'dialog-close'));dialog.append(head,body);document.body.append(dialog);dialog.addEventListener('close',()=>{configChanged=null;dialog.remove();});dialog.showModal();body.append(el('p','review-job-help','正在核对当前改稿与本轮记录…'));
    const error=message=>{body.querySelector('.review-job-error')?.remove();const p=el('p','review-job-error',message);p.setAttribute('role','alert');body.prepend(p);};
    try{
      const [preflight,caps]=await Promise.all([request(endpoint(bundle.id)),request('/api/review-capabilities')]);if(!dialog.open||current?.id!==bundle.id)return;
      body.replaceChildren();if(!preflight.available){body.append(el('p','review-job-help',preflight.reason||'这轮缺少可复审的原始 DOCX。'),btn('关闭',()=>dialog.close()));return;}
      await window.APIProfiles?.load().catch(()=>{});
      const form=el('form','review-job-form');let inputRevision=preflight.input_revision,submitting=false,preflightAvailable=true;
      form.append(el('h3','',preflight.document_name||bundle.report?.document_name||'当前合同'),el('p','review-job-help','讨论和修改完成后，可以让 AI 再检查一遍全文。按这次清单和审查要求，结合确认保存的信息检查改后合同。'),el('p','review-job-help','复审生成新报告，原报告和修改记录保留。选择过“不采用修改建议”的问题也会重新检查。讨论中要用于复审的信息，请先保存到“审查信息”。'));
      const goal=bundle.report?.context?.business_goal;if(goal){const detail=el('details','review-job-scope-details');detail.append(el('summary','','本次审查要求'),el('p','review-job-excerpt',goal));form.append(detail);}
      const available=(caps.engines||[]).filter(engine=>engine.available),engineBox=el('div','review-job-engine'),modelHost=el('div','review-model-selector');let selector=null,consentFingerprint;
      engineBox.append(el('p','','合同文字将发送到下面的模型服务'),modelHost);
      const research=el('label','review-job-consent'),web=el('input');web.type='checkbox';web.checked=true;research.append(web,el('span','','联网查证相关依据'));
      const consent=el('label','review-job-consent'),check=el('input');check.type='checkbox';consent.append(check,el('span','','我已完成这轮讨论和修改，同意将改后合同、清单、审查要求和已保存的信息发送给所选模型，开始全文复审。'));
      const submit=el('button','review-job-primary','开始全文复审');submit.type='submit';const actions=el('div','review-job-actions');actions.append(btn('取消',()=>dialog.close()),submit);
      engineBox.append(research);form.append(engineBox,consent,actions);body.append(form);
      const update=()=>{
        const chosen=selector?.value(),engine=available.find(item=>item.key===chosen?.engine_key);
        const fingerprint=JSON.stringify(chosen?[chosen.engine_key,chosen.provider||'',chosen.config?.base_url||'',chosen.config?.api_key||'',chosen.config?.model||'']:null);
        if(consentFingerprint!==undefined&&consentFingerprint!==fingerprint)check.checked=false;
        consentFingerprint=fingerprint;
        let searchAllowed=false;try{const url=new URL(chosen?.config?.base_url);searchAllowed=engine?.key==='compatible_api'&&url.protocol==='https:'&&url.hostname==='api.deepseek.com'&&(!url.port||url.port==='443')&&['','/v1','/chat/completions','/v1/chat/completions'].includes(url.pathname.replace(/\/+$/,''));}catch{}
        research.hidden=!searchAllowed;web.disabled=!searchAllowed;submit.disabled=!preflightAvailable||submitting||!engine||engine.key==='compatible_api'&&!chosen.config?.model;
      };
      if(window.ReviewModelSelector){selector=window.ReviewModelSelector.mount(modelHost,caps.engines||[],update);await selector.ready;}
      else {const profile=window.APIProfiles?.active(),engine=(profile?available.find(e=>e.key==='compatible_api'):null)||available.find(e=>e.key==='codex_cli')||available[0];selector={value:()=>engine?{engine_key:engine.key,provider:profile?.provider,config:engine.key==='compatible_api'?profile?.config||{}:{}}:null};modelHost.append(el('p','',engine?.key==='codex_cli'?'Sol':profile?.config?.model||'尚未配置模型'));}
      configChanged=()=>{if(form.isConnected&&dialog.open){selector?.refresh?.();update();}};update();
      form.addEventListener('submit',async event=>{
        event.preventDefault();if(submitting)return;if(!check.checked)return error('请先核对所选模型，并勾选同意发送复审材料。');const choice=selector?.value(),engine=available.find(item=>item.key===choice?.engine_key),config=choice?.config;if(!engine||!config)return error('当前模型尚未配置，请先保存模型设置。');
        const key=JSON.stringify([bundle.id,inputRevision,engine.key,config,!research.hidden&&web.checked]);let requestId=attempts.get(key);if(!requestId){requestId=globalThis.crypto.randomUUID();attempts.set(key,requestId);}
        submitting=true;submit.disabled=true;submit.textContent='正在启动…';
        try{const data=await request(endpoint(bundle.id),{input_revision:inputRevision,request_id:requestId,engine_key:engine.key,config,consent:true,web_search:!research.hidden&&web.checked});dialog.close();await window.ReviewJobsUI.openJob(data.job.job_id);}
        catch(reason){if(reason.status===409){attempts.delete(key);check.checked=false;try{const fresh=await request(endpoint(bundle.id));if(fresh.available){inputRevision=fresh.input_revision;error('复审材料已更新，本次未启动。请核对当前修改与信息，再重新确认。');}else {preflightAvailable=false;error(fresh.reason||'当前已不能复审。');submit.disabled=true;}}catch{error(reason.message);}}else error(reason.message);}
        finally{submitting=false;submit.textContent='开始全文复审';if(dialog.open)update();}
      });
    }catch(reason){error(reason.message);}finally{opening=false;}
  }
  document.getElementById('rereview')?.addEventListener('click',open);
  window.addEventListener('api-profiles-changed',()=>configChanged?.());
  window.RereviewUI={load,open};
})();
