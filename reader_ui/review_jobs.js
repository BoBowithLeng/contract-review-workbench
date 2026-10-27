(()=>{
  'use strict';

  const dialog=document.getElementById('new-review-dialog');
  const body=document.getElementById('new-review-body');
  const openButton=document.getElementById('new-review');
  const sessionKey='contract-review-pending-job';
  const rememberedRolePrefix='contract-review-role:';
  const fallbackLimit=12*1024*1024;
  let capabilities=null;
  let pendingJobId=readPendingId();
  let pollTimer=null;
  let onComplete=()=>{};
  let refreshReadyConfig=null;
  const delivered=new Set();
  let intakeDraft=null;

  function node(tag,className,text){
    const element=document.createElement(tag);
    if(className)element.className=className;
    if(text!==undefined)element.textContent=text;
    return element;
  }

  function button(text,className,handler){
    const element=node('button',className,text);
    element.type='button';
    if(handler)element.addEventListener('click',handler);
    return element;
  }

  function readPendingId(){
    try{return sessionStorage.getItem(sessionKey)||'';}catch(_error){return '';}
  }

  function rememberPending(id){
    pendingJobId=id||'';
    try{
      if(id)sessionStorage.setItem(sessionKey,id);
      else sessionStorage.removeItem(sessionKey);
    }catch(_error){}
  }

  function rememberedRole(profileKey,roles){
    try{
      const value=localStorage.getItem(rememberedRolePrefix+profileKey)||'';
      return roles.some(role=>role.key===value)?value:'';
    }catch(_error){return '';}
  }

  function saveRememberedRole(profileKey,role){
    try{localStorage.setItem(rememberedRolePrefix+profileKey,role);}catch(_error){}
  }

  function clearRememberedRole(profileKey){
    try{localStorage.removeItem(rememberedRolePrefix+profileKey);}catch(_error){}
  }

  async function request(path,options={}){
    let response;
    try{response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});}
    catch(_error){throw new Error('无法连接本机审核服务，请确认阅读台仍在运行。');}
    let data={};
    try{data=await response.json();}catch(_error){}
    if(!response.ok){
      const error=new Error(data.error||('请求失败（'+response.status+'）'));
      error.status=response.status;
      throw error;
    }
    return data;
  }

  function showError(message,container=body){
    container.querySelector('.review-job-error')?.remove();
    const error=node('p','review-job-error',message||'操作失败，请检查后重试。');
    error.setAttribute('role','alert');
    container.prepend(error);
  }

  function setBody(...children){
    body.replaceChildren(...children.filter(Boolean));
  }

  function setJobTitle(job=null){
    const title=document.getElementById('new-review-title');
    if(title)title.textContent=!job?'新建审查':job.review_kind==='rereview'||job.parent_report_id?'合同复审':'合同审查';
  }

  // Which side we are on is the stance field; how hard to push is said here, as one
  // sentence at the top of the review requirement the model already reads.
  const REVIEW_POSTURES=[
    ['均衡对等','审查尺度：均衡对等。站在我方立场，但改稿以双方权责对等为准，适合重视合同形象、准备长期合作的情形。'],
    ['尽量争取我方','审查尺度：尽量争取我方。在合法、对方大概率能接受的范围内，尽量争取对我方有利的安排。'],
    ['只看重大风险','审查尺度：只看重大风险。只指出会造成明显损失或纠纷的问题，措辞细节不必逐条修改。'],
  ];
  function postureChoices(goal){
    const box=node('div','review-job-posture wide');
    box.append(node('span','review-job-posture-label','审查尺度'));
    for(const [label,sentence] of REVIEW_POSTURES){
      const choice=node('button','review-job-posture-choice',label);choice.type='button';
      choice.onclick=()=>{
        const rest=goal.value.replace(/^审查尺度：[^\n]*\n?/,'');
        goal.value=(sentence+(rest?'\n'+rest:'')).slice(0,goal.maxLength>0?goal.maxLength:4000);
        for(const other of box.querySelectorAll('button'))other.setAttribute('aria-pressed',String(other===choice));
      };
      choice.setAttribute('aria-pressed','false');box.append(choice);
    }
    box.append(node('small','','点一下填进“本次审查要求”开头，可以再改。'));
    return box;
  }
  function field(labelText,input,help='',wide=false){
    const label=node('label','review-job-field'+(wide?' wide':''));
    label.append(node('span','',labelText),input);
    if(help)label.append(node('small','',help));
    return label;
  }

  function input(type,name,value=''){
    const element=node('input');
    element.type=type;
    element.name=name;
    element.value=value;
    return element;
  }

  async function loadCapabilities(){
    if(!capabilities)capabilities=await request('/api/review-capabilities',{method:'GET',headers:{}});
    return capabilities;
  }

  function fileBase64(file){
    return new Promise((resolve,reject)=>{
      const reader=new FileReader();
      reader.onerror=()=>reject(new Error('无法读取这个文件，请重新选择。'));
      reader.onload=()=>resolve(String(reader.result).split(',',2)[1]||'');
      reader.readAsDataURL(file);
    });
  }

  function contextValue(defaults,key,fallback){
    const value=defaults?.[key];
    return value===undefined||value===null?'':String(value||fallback);
  }

  function availableProfiles(caps){
    if(Array.isArray(caps.profiles)&&caps.profiles.length)return caps.profiles;
    return [{key:'labor',label:'劳动合同',roles:[],default_context:caps.default_context||{},scope_note:'当前审核范围为劳动合同。'}];
  }

  function partySummary(context={}){
    const roleKey=context.our_role||'';
    const profile=availableProfiles(capabilities||{}).find(item=>item.key===(context.contract_type||'labor'));
    const role=profile?.roles?.find(item=>item.key===roleKey);
    const fallback={client:'客户',provider:'服务商',employer:'用人单位',employee:'劳动者'}[roleKey];
    const label=fallback||role?.label||context.our_role_label||(context.contract_type==='general'?roleKey:'');
    return [label,context.our_party].filter(Boolean).join('·');
  }

  function jobContext(job={},preview={}){
    return {
      ...(preview.context||{}),...(job.context||{}),
      contract_type:job.contract_type||job.profile_key||job.context?.contract_type||preview.context?.contract_type,
      our_role:job.our_role||job.context?.our_role||preview.context?.our_role,
      our_party:job.our_party||job.context?.our_party||preview.context?.our_party,
    };
  }

  async function renderIntake(){
    setJobTitle();
    setBody(node('p','review-job-help','正在读取可用审核方式…'));
    let caps;
    try{caps=await loadCapabilities();}catch(error){showError(error.message);return;}
    const profiles=availableProfiles(caps);
    const general=profiles.find(profile=>profile.key==='general');
    if(general)return renderGeneralIntake(caps,general);
    const initialProfile=profiles.find(item=>item.key==='labor')||profiles[0];
    const maxBytes=Number(caps.limits?.docx_bytes)||fallbackLimit;
    const form=node('form','review-job-form');form.autocomplete='off';form.noValidate=true;
    const file=input('file','document');
    file.accept='.docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document';
    file.required=true;
    const profileSelect=node('select');profileSelect.name='contract_type';profileSelect.required=true;
    profiles.forEach(profile=>{const option=node('option','',profile.label);option.value=profile.key;profileSelect.append(option);});
    profileSelect.value=initialProfile.key;
    const roleSelect=node('select');roleSelect.name='our_role';
    const laborRole=input('text');laborRole.value='用人单位';laborRole.readOnly=true;laborRole.setAttribute('aria-readonly','true');
    const ourParty=input('text','our_party');ourParty.maxLength=200;ourParty.placeholder='例如：甲方、丙方或合同中的公司名称';
    const region=input('text','region'); region.required=true;
    const reviewDate=input('date','review_date'); reviewDate.required=true;
    const kind=node('select'); kind.name='input_kind';
    ['模板审阅','拟签审阅'].forEach(value=>{const option=node('option','',value);option.value=value;kind.append(option);});
    const goal=node('textarea');goal.name='business_goal';goal.maxLength=4000;goal.placeholder='例如：准备与新员工签约，希望减少离职交接争议';
    const roleControl=node('span','review-job-role-control');
    const roleField=field('我方立场',roleControl);
    const rememberRole=node('label','review-job-remember-role');
    const rememberRoleInput=input('checkbox','remember_role');
    rememberRole.append(rememberRoleInput,node('span','','记住此类合同的立场'));
    const stanceGroup=node('div','review-job-stance-group');stanceGroup.append(roleField,rememberRole);
    const partyField=field('我方在合同中的称谓',ourParty);
    const mainFields=node('div','review-job-context review-job-main-fields');
    mainFields.append(field('合同类型',profileSelect),stanceGroup,partyField,field('本次审查要求（可选）',goal,'',true));
    const settings=node('details','review-job-settings');
    settings.append(node('summary','','审查设置'));
    const settingsFields=node('div','review-job-context');
    settingsFields.append(field('适用地区',region),field('审核基准日',reviewDate),field('合同状态',kind));
    settings.append(settingsFields);
    const applyProfile=()=>{
      const profile=profiles.find(item=>item.key===profileSelect.value)||profiles[0];
      const defaults={...(caps.default_context||{}),...(profile.default_context||{})};
      region.value=contextValue(defaults,'region','上海');
      reviewDate.value=contextValue(defaults,'review_date','');
      kind.value=contextValue(defaults,'input_kind','模板审阅');
      roleSelect.replaceChildren();roleSelect.value='';
      const roles=profile.roles||[];
      const placeholder=node('option','',roles.length?'请选择我方立场':'');
      placeholder.value='';placeholder.selected=true;placeholder.disabled=roles.length>0;roleSelect.append(placeholder);
      for(const role of roles){const option=node('option','',role.label);option.value=role.key;roleSelect.append(option);}
      const service=profile.key==='software_service';
      roleControl.replaceChildren(service?roleSelect:laborRole);
      roleSelect.required=service;
      const savedRole=service?rememberedRole(profile.key,roles):'';
      if(service)roleSelect.value=savedRole;
      rememberRoleInput.checked=Boolean(savedRole);
      rememberRole.hidden=!service;
      partyField.hidden=!service;ourParty.required=service;if(!service)ourParty.value='';
      goal.placeholder=service?'例如：希望明确交付验收和延期责任':'例如：准备与新员工签约，希望减少离职交接争议';
    };
    profileSelect.addEventListener('change',applyProfile);applyProfile();
    const actions=node('div','review-job-actions');
    const read=button('读取合同','review-job-primary');read.type='submit';actions.append(read);
    form.append(field('合同文件',file,'DOCX，最大 '+Math.round(maxBytes/1024/1024)+' MB。'),mainFields,settings,actions);
    form.addEventListener('submit',async event=>{
      event.preventDefault();
      form.querySelector('.review-job-error')?.remove();
      const selected=file.files?.[0];
      const revealInvalid=(control,message)=>{
        const details=control.closest('details');if(details)details.open=true;
        if(message)showError(message,form);
        control.focus();control.reportValidity();
      };
      if(!selected)return revealInvalid(file,'请选择一个 DOCX 文件。');
      if(!selected.name.toLowerCase().endsWith('.docx'))return revealInvalid(file,'只支持 DOCX 文件。');
      if(selected.size>maxBytes)return revealInvalid(file,'文件超过 '+Math.round(maxBytes/1024/1024)+' MB，无法读取。');
      const invalid=[...form.querySelectorAll('input[required],select[required],textarea[required]')]
        .find(control=>!control.checkValidity());
      if(invalid)return revealInvalid(invalid,'请完整填写标记的必填项。');
      read.disabled=true;read.textContent='正在读取…';
      try{
        const docx=await fileBase64(selected);
        const profile=profiles.find(item=>item.key===profileSelect.value)||profiles[0];
        const reviewContext={
          contract_type:profile.key,
          region:region.value.trim(),review_date:reviewDate.value,input_kind:kind.value,
          business_goal:goal.value.trim()
        };
        if(profile.key==='software_service'){
          reviewContext.our_role=roleSelect.value;
          reviewContext.our_party=ourParty.value.trim();
        }
        const data=await request('/api/jobs',{method:'POST',body:JSON.stringify({
          document_name:selected.name,
          docx_base64:docx,
          context:reviewContext
        })});
        if(profile.key==='software_service'){
          if(rememberRoleInput.checked)saveRememberedRole(profile.key,roleSelect.value);
          else clearRememberedRole(profile.key);
        }
        rememberPending(data.job.job_id);
        renderReady(data.job,data.preview);
      }catch(error){showError(error.message,form);read.disabled=false;read.textContent='读取合同';}
    });
    setBody(form);
  }

  function renderGeneralIntake(caps,profile){
    const defaults={...(caps.default_context||{}),...(profile.default_context||{})};
    const maxBytes=Number(caps.limits?.docx_bytes)||fallbackLimit;
    const form=node('form','review-job-form');form.autocomplete='off';form.noValidate=true;
    const file=input('file','document');file.accept='.docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document';file.required=true;
    const type=input('text','contract_type_label');type.maxLength=200;type.placeholder='例如：采购合同、房屋租赁合同';
    const party=input('text','our_party');party.required=true;party.maxLength=200;party.placeholder='例如：甲方、丙方或合同中的公司名称';
    const role=input('text','our_role');role.maxLength=100;role.placeholder='例如：采购方、出租方、用人单位';
    const goal=node('textarea');goal.name='business_goal';goal.maxLength=4000;goal.placeholder='例如：重点检查提前终止后的结算，尽量保留我方解除权';
    const facts=node('textarea');facts.name='business_facts';facts.maxLength=8000;facts.placeholder='例如：价款分三次支付，项目目前尚未开工。只填已知信息，不需要提前回答所有问题。';
    const region=input('text','region',contextValue(defaults,'region','中国大陆'));region.required=true;
    const date=input('date','review_date',contextValue(defaults,'review_date',''));date.required=true;
    const kind=node('select');kind.name='input_kind';
    for(const name of ['模板审阅','拟签审阅','已签复核']){const option=node('option','',name);option.value=name;kind.append(option);}
    kind.value=defaults.input_kind||'拟签审阅';
    const fields=node('div','review-job-context review-job-main-fields');
    fields.append(field('合同类型（可选）',type),field('我方在合同中的称谓或主体',party),field('我方角色（可选）',role),postureChoices(goal),field('本次审查要求（可选）',goal,'说明希望重点检查什么、争取什么安排。',true),field('已知交易信息（可选）',facts,'只填写这份合同相关的已知事实；真正缺少必要信息时，会在审核前再问。',true));
    const checklistHost=node('div');
    let intakeScroll=0,intakeFocus=null;
    const picker=window.ChecklistsUI?.mountPicker(checklistHost,{openStep:render=>{
      if(!render){setBody(form);intakeFocus?.focus?.({preventScroll:true});dialog.scrollTop=intakeScroll;return;}
      intakeScroll=dialog.scrollTop||0;intakeFocus=document.activeElement;
      const step=node('section','review-checklist-step');setBody(step);render(step);
      const headHeight=dialog.querySelector('.dialog-head')?.offsetHeight;
      if(headHeight)dialog.style?.setProperty('--review-dialog-head-height',headHeight+'px');
      dialog.scrollTop=0;body.scrollTop=0;
    }});
    const settings=node('details','review-job-settings');settings.append(node('summary','','审查设置'));
    const extra=node('div','review-job-context');extra.append(field('适用地区',region),field('审核基准日',date),field('合同状态',kind));settings.append(extra);
    const read=button('读取合同','review-job-primary');read.type='submit';const actions=node('div','review-job-actions');actions.append(read);
    form.append(field('合同文件',file,'DOCX，最大 '+Math.round(maxBytes/1024/1024)+' MB。'),fields,checklistHost,settings,actions);
    intakeDraft={form,signature:null,job:null,preview:null};
    form.addEventListener('submit',async event=>{
      event.preventDefault();form.querySelector('.review-job-error')?.remove();
      const selected=file.files?.[0];
      const invalid=[...form.querySelectorAll('input[required]')].find(control=>!control.checkValidity());
      if(invalid){invalid.closest('details')?.setAttribute('open','');showError(invalid===party?'请填写我方在合同中的称谓或主体。':'请完整填写必填项。',form);invalid.focus();invalid.reportValidity();return;}
      if(!selected||!selected.name.toLowerCase().endsWith('.docx'))return showError('请选择一个 DOCX 文件。',form);
      if(selected.size>maxBytes)return showError('文件超过 '+Math.round(maxBytes/1024/1024)+' MB，无法读取。',form);
      read.disabled=true;read.textContent='正在读取…';
      try{
        await picker?.ready;
        const snapshot=picker?.snapshot();if(!snapshot)throw new Error('请选择检查清单，或重试读取清单库。');
        const payload={document_name:selected.name,docx_base64:await fileBase64(selected),checklist_snapshot:snapshot,context:{
          contract_type:'general',contract_type_label:type.value.trim(),our_party:party.value.trim(),our_role:role.value.trim(),
          region:region.value.trim(),review_date:date.value,input_kind:kind.value,business_goal:goal.value.trim(),business_facts:facts.value.trim()
        }};
        const signature=JSON.stringify(payload);
        if(intakeDraft.signature===signature&&intakeDraft.job){rememberPending(intakeDraft.job.job_id);await renderReady(intakeDraft.job,intakeDraft.preview);}
        else {const data=await request('/api/jobs',{method:'POST',body:JSON.stringify(payload)});intakeDraft.signature=signature;intakeDraft.job=data.job;intakeDraft.preview=data.preview;rememberPending(data.job.job_id);await renderReady(data.job,data.preview);}
        read.disabled=false;read.textContent='读取合同';
      }catch(error){showError(error.message,form);read.disabled=false;read.textContent='读取合同';}
    });
    setBody(form);
  }

  function previewBlock(job,preview){
    const wrap=node('section','review-job-preview');wrap.append(node('h3','',job.document_name||'合同已读取'));
    const identity=partySummary(jobContext(job,preview));if(identity)wrap.append(node('p','review-job-party',identity));
    const warnings=(preview?.warnings||[]).filter(text=>!/^已忽略 .+ 中仅用于显示页码的 PAGE\/NUMPAGES 字段。$/.test(text));
    if(warnings.length){const list=node('ul','review-job-warning');warnings.forEach(text=>list.append(node('li','',text)));wrap.append(list);}
    return wrap;
  }

  async function renderReady(job,preview=null){
    setJobTitle(job);
    if(intakeDraft?.job?.job_id===job.job_id){intakeDraft.job=job;if(preview)intakeDraft.preview=preview;}
    const newWorkflow=Number(job.workflow_version)>=2;
    if(newWorkflow&&job.status==='needs_info')return renderQuestions(job,preview);
    const reviewRetry=job.status==='failed'&&job.retryable===true;
    const isPrepared=newWorkflow&&(job.status==='prepared'||reviewRetry&&job.preparation?.status==='prepared');
    const isPreparing=newWorkflow&&!isPrepared&&!reviewRetry;
    const retry=isPreparing&&job.preparation?.status==='failed';
    let submitting=false,needsRefresh=false;
    let caps;
    try{caps=await loadCapabilities();}catch(error){setBody(previewBlock(job,preview));showError(error.message);return;}
    if(window.APIProfiles)await window.APIProfiles.load().catch(()=>{});
    const available=(caps.engines||[]).filter(engine=>engine.available);
    const form=node('form','review-job-form');form.autocomplete='off';
    const engineBox=node('div','review-job-engine');
    const engineLabel=node('p','','合同文字将发送到下面的模型服务');
    const modelHost=node('div','review-model-selector');let selector=null;
    const research=node('label','review-job-consent');
    const researchInput=input('checkbox','web_search');researchInput.checked=true;
    research.append(researchInput,node('span','','联网查证：先检索相关依据，再生成审核意见'));
    const consent=node('label','review-job-consent');
    const consentInput=input('checkbox','consent');
    consent.append(consentInput,node('span','',isPreparing?'我同意将合同、审查要求与已填信息发送给所选模型，检查准备情况。':reviewRetry?'我同意将保留的审查材料发送给所选模型，重新尝试这次审查。':'我同意将本次审查材料发送给所选模型，现在开始审核。'));
    const actions=node('div','review-job-actions');
    const back=button('换一份合同','review-job-secondary',()=>{rememberPending('');renderIntake();});
    const actionLabel=isPreparing?(retry?'重试检查准备情况':'检查准备情况'):reviewRetry?'重新尝试':'开始审核';
    const start=button(actionLabel,'review-job-primary');start.type='submit';
    if(intakeDraft?.job?.job_id===job.job_id)actions.append(button('返回上一步','review-job-secondary',()=>{refreshReadyConfig=null;setBody(intakeDraft.form);}));
    actions.append(back,start);
    engineBox.append(engineLabel,modelHost,research);
    form.append(previewBlock(job,preview));
    if(reviewRetry)form.append(node('p','review-job-help','合同、清单和已填信息都还在，不需要重新上传。请选择模型并重新勾选同意，再点击“重新尝试”。'));
    if(isPreparing){form.append(node('h3','preparation-heading','先检查准备情况'),node('p','review-job-help','AI会先读合同，查看是否需要你补充信息。这一步还不生成审查报告。'));
      if(retry)form.append(node('p','review-job-error',job.preparation.error||'准备检查没有完成。请选好模型，再点击“重试检查准备情况”。'));

    }
    if(isPrepared){
      form.append(preparationPlan(job));
      if(job.preparation?.questions?.length&&!reviewRetry&&!job.attempt&&job.review_kind!=='rereview'&&!job.parent_report_id)form.append(button('修改补充信息','review-job-secondary',()=>renderQuestions(job,preview)));
    }
    form.append(engineBox,consent,actions);

    let consentFingerprint;
    const renderEngine=()=>{
      const chosen=selector?.value();
      const fingerprint=JSON.stringify(chosen?[chosen.engine_key,chosen.provider||'',chosen.config?.base_url||'',chosen.config?.api_key||'',chosen.config?.model||'']:null);
      if(consentFingerprint!==undefined&&fingerprint!==consentFingerprint)consentInput.checked=false;
      consentFingerprint=fingerprint;
      const engine=chosen&&(caps.engines||[]).find(item=>item.key===chosen.engine_key&&item.available);
      start.disabled=submitting||needsRefresh||!engine||(engine.key==='compatible_api'&&!chosen.config?.model);
      let searchAllowed=false;try{const url=new URL(chosen?.config?.base_url);searchAllowed=engine?.key==='compatible_api'&&jobContext(job,preview).contract_type==='general'&&url.protocol==='https:'&&url.hostname==='api.deepseek.com'&&(!url.port||url.port==='443')&&['','/v1','/chat/completions','/v1/chat/completions'].includes(url.pathname.replace(/\/+$/,''));}catch{}
      research.hidden=isPreparing||!searchAllowed;researchInput.disabled=isPreparing||!searchAllowed;
    };
    if(window.ReviewModelSelector){selector=window.ReviewModelSelector.mount(modelHost,caps.engines||[],renderEngine);await selector.ready;}
    else {const engine=available.find(e=>e.key==='codex_cli')||available[0];selector={value:()=>engine?{engine_key:engine.key,config:window.APIProfiles?.active()?.config||{}}:null};modelHost.append(node('p','',engine?.key==='codex_cli'?'Sol':window.APIProfiles?.active()?.config?.model||'尚未配置模型'));}
    renderEngine();
    form.addEventListener('submit',async event=>{
      event.preventDefault();
      if(submitting||needsRefresh)return;
      form.querySelector('.review-job-error')?.remove();
      if(!consentInput.checked)return showError('请先确认所选模型，并勾选同意发送审查材料。',form);
      const choice=selector?.value();const engine=(caps.engines||[]).find(item=>item.key===choice?.engine_key&&item.available);
      if(!engine)return showError('请选择当前可用的审核方式。',form);
      const values=choice?.config;
      if(!values)return showError('请先在模型设置中保存配置。',form);
      if(!form.reportValidity())return;
      submitting=true;start.disabled=true;start.textContent=isPreparing?'正在检查准备情况…':'正在启动…';
      try{
        const data=await request('/api/jobs/'+encodeURIComponent(job.job_id)+(isPreparing?'/prepare':'/start'),{method:'POST',body:JSON.stringify({engine_key:engine.key,consent:true,config:values,...(reviewRetry?{retry:true,expected_attempt:job.attempt}:retry?{retry:true}:{}),web_search:!research.hidden&&researchInput.checked})});
        const updated=data.job||{...job,status:isPreparing?'preparing':'running',engine_key:engine.key};
        if(['running','preparing'].includes(updated.status)){renderProgress(updated);schedulePoll(job.job_id,0);}
        else if(updated.status==='completed')await finish(updated);
        else if(updated.status==='failed'){if(newWorkflow&&updated.preparation?.status==='failed')await renderReady(updated,preview);else renderFailure(updated,preview);}
        else await renderReady(updated,preview);
      }catch(error){
        showError(error.status===409?'这次审查已在另一处更新。请点击“重新读取任务”查看当前进度，再决定是否重试。':error.message,form);
        if(error.status===409){needsRefresh=true;consentInput.checked=false;start.disabled=true;form.append(button('重新读取任务','review-job-secondary',()=>checkJob(job.job_id,{show:true})));}
        else start.disabled=false;
        start.textContent=actionLabel;
      }finally{submitting=false;}
    });
    setBody(form);
    refreshReadyConfig=()=>{if(form.isConnected&&dialog.open){selector?.refresh?.();renderEngine();}};
  }

  function preparationPlan(job){
    const wrap=node('section','preparation-plan'),prep=job.preparation||{};
    wrap.append(node('h3','preparation-heading','准备完成'),node('p','review-job-help','已填信息会用于这次审查，暂时无法提供的问题仍列为待确认。确认后开始正式审查。'));
    if(prep.requirements?.length){const detail=node('details','review-job-scope-details');detail.append(node('summary','','这次审查要求会怎样检查'));
      for(const requirement of prep.requirements){const item=node('div','preparation-requirement'),matched=(requirement.related_check_ids||[]).map(id=>prep.checks?.find(c=>c.id===id)?.title||id);item.append(node('p','',requirement.source_text),node('small','',({merged:'结合清单中的相关检查项',added:'单独检查这项要求',context:'作为背景信息或处理偏好参考'})[requirement.disposition]||'会用于这次审查'));if(matched.length)item.append(node('p','',matched.join('、')));if(requirement.reason)item.append(node('p','review-job-help',requirement.reason));detail.append(item);}wrap.append(detail);
    }
    if(prep.questions?.length){const answers=node('details','review-job-scope-details');answers.append(node('summary','','本次合同的补充信息'));for(const q of prep.questions)answers.append(node('h4','',q.question),node('p','',q.status==='answered'?q.answer:'暂时无法提供'));wrap.append(answers);}
    return wrap;
  }

  function renderQuestions(job,preview){
    setJobTitle(job);
    const form=node('form','review-job-form');form.autocomplete='off';form.noValidate=true;
    form.className+=' preparation-answers';form.append(node('h3','preparation-heading','需要补充的信息'),node('p','review-job-help','已填信息会用于审查。暂时无法提供也可以保存并继续，这些问题会列为待确认。'));
    const answers=[];
    for(const q of job.preparation?.questions||[]){const section=node('section','preparation-question');const where=job.preview?.locations||preview?.locations||{};
      const reason=q.reason?(window.ReaderText?.humanize(q.reason,{locations:where,inline_locations:where},{plain:true,shortLocations:true,hideUnknown:true})||q.reason):'请补充这份合同相关的信息。';
      section.append(node('h4','',q.question),node('p','checklist-muted',reason));const value=node('textarea');value.name='answer_'+q.id;value.maxLength=8000;value.value=q.answer||'';value.setAttribute('aria-label',q.question);const unknown=input('checkbox','unknown_'+q.id);unknown.checked=q.status==='unknown'&&job.status==='prepared';value.disabled=unknown.checked;const choice=node('label');choice.append(unknown,node('span','','暂时无法提供'));unknown.addEventListener('change',()=>{value.disabled=unknown.checked;});section.append(value,choice);form.append(section);answers.push({question:q,value,unknown});}
    const actions=node('div','review-job-actions'),submit=button('保存并继续','review-job-primary');submit.type='submit';actions.append(button('暂不补充，继续','review-job-secondary',()=>{answers.forEach(item=>{if(!item.value.value.trim()){item.unknown.checked=true;item.value.disabled=true;}});form.dispatchEvent(new Event('submit',{cancelable:true}));}),button('重新读取任务','review-job-secondary',()=>checkJob(job.job_id,{show:true})),submit);form.append(actions);
    form.addEventListener('submit',async event=>{event.preventDefault();if(submit.disabled)return;form.querySelector('.review-job-error')?.remove();submit.disabled=true;try{const payload=answers.map(item=>({id:item.question.id,answer:item.unknown.checked?'':item.value.value.trim(),status:item.unknown.checked||!item.value.value.trim()?'unknown':'answered'}));const data=await request('/api/jobs/'+encodeURIComponent(job.job_id)+'/answers',{method:'POST',body:JSON.stringify({expected_revision:job.preparation.revision,answers:payload})});await renderReady(data.job,preview);if(data.job.status==='needs_info')showError('回答已保存，准备状态尚未更新，请重新读取任务。');}catch(error){showError(error.status===409?'这份任务已在另一处更新。当前输入仍在，请重新读取任务后再补充。':error.message,form);}finally{submit.disabled=false;}});
    setBody(form);refreshReadyConfig=null;
  }

  function renderProgress(job){
    setJobTitle(job);
    const wrap=node('section','review-job-progress');
    wrap.append(node('h3','',job.document_name||'正在审核合同'));
    const status=node('p','review-job-status');status.setAttribute('role','status');status.append(node('span','review-job-spinner'),node('span','',job.status==='preparing'?'正在检查准备情况，只整理检查项与必要问题，不生成完整报告。可以关闭窗口，之后继续补充信息。':job.research_status==='pending'?'正在检索相关依据，之后生成审核意见。可以关闭窗口，任务会继续运行。':'模型正在审核。可以关闭窗口继续看其他合同，完成后会提示你查看结果。'));
    if(job.status==='preparing'){wrap.className+=' preparation-reading';status.replaceChildren(node('span','preparation-breath'));status.append(node('span','','模型正在阅读全文，确认哪些信息需要先问清。'));wrap.append(status,node('p','review-job-help','准备完成后，会显示必要问题；没有需要补问的内容时，可直接确认开始正式审查。'));}else wrap.append(status);
    setBody(wrap);
    refreshReadyConfig=null;
  }

  function failureExplanation(job){
    const error=String(job.error||'');
    if(job.failure_type==='ModelClientError'&&!job.failure_diagnostics)return '上次调用未完成，具体原因未记录。合同和信息已保留。';
    if(/结果校验|evidence_status|quote|source_ids|validation|schema/i.test(error))return '模型的回复还不能整理成完整报告。合同和已填信息都还在。';
    return error||'这次审核没有完成。合同和信息已保留。';
  }
  function renderFailure(job,preview=null){
    setJobTitle(job);
    const wrap=node('section','review-job-progress');
    if(job.report_id){
      wrap.append(node('h3','','部分检查尚未完成'),node('p','review-job-help','报告已生成，但仍有事项没检查完。请打开报告核对这些事项。'));
      const actions=node('div','review-job-actions');
      actions.append(button('查看报告','review-job-primary',async()=>{try{await onComplete(job.report_id);rememberPending('');if(dialog.open)dialog.close();}catch(error){showError(error.message,wrap);}}));
      wrap.append(actions);
    }else{
      wrap.append(node('h3','','审核没有完成'),node('p','review-job-error',failureExplanation(job)));
      const actions=node('div','review-job-actions');
      if(job.retryable)actions.append(button('重新尝试','review-job-primary',()=>renderReady(job,preview)));
      actions.append(button('换一份合同','review-job-secondary',()=>{rememberPending('');renderIntake();}));wrap.append(actions);
    }
    setBody(wrap);refreshReadyConfig=null;
  }

  async function finish(job){
    setJobTitle(job);
    if(delivered.has(job.job_id))return;
    delivered.add(job.job_id);
    const wrap=node('section','review-job-progress');
    const resultText='审查结果已生成，正在打开报告…';
    wrap.append(node('h3','','正在打开审查结果'),node('p','review-job-complete',resultText));
    setBody(wrap);
    try{
      const canNavigate=()=>dialog.open&&pendingJobId===job.job_id;
      const opened=await onComplete(job.report_id||job.job_id,{automatic:true,canNavigate,documentName:job.document_name});
      if(opened!==false&&canNavigate())dialog.close();
      if(pendingJobId===job.job_id)rememberPending('');
    }catch(error){delivered.delete(job.job_id);showError(error?.message||'结果已生成，但页面暂时无法打开。');}
  }

  async function checkJob(id,{show=false}={}){
    try{
      const data=await request('/api/jobs/'+encodeURIComponent(id),{method:'GET',headers:{}});
      if(id!==pendingJobId)return;
      const job=data.job;
      if(job.status==='completed')return finish(job);
      if(job.status==='failed'){if(show||dialog.open){if(Number(job.workflow_version)>=2&&job.preparation?.status==='failed')return renderReady(job,data.preview||job.preview);renderFailure(job,data.preview||job.preview);}return;}
      if(['running','preparing'].includes(job.status)){
        if(show||dialog.open)renderProgress(job);
        schedulePoll(id,3000);
        return;
      }
      if(['ready','needs_info','prepared'].includes(job.status)&&(show||dialog.open))return renderReady(job,data.preview||job.preview);
    }catch(error){
      if(error.status===404)rememberPending('');
      if(show||dialog.open){showError(error.message);if(error.status!==404)body.append(button('重新读取任务','review-job-secondary',()=>checkJob(id,{show:true})));}
    }
  }

  function schedulePoll(id,delay=3000){
    clearTimeout(pollTimer);
    pollTimer=setTimeout(()=>checkJob(id),delay);
  }

  async function open(){
    if(!dialog.open)dialog.showModal();
    clearTimeout(pollTimer);rememberPending('');intakeDraft=null;
    return renderIntake();
  }

  async function openJob(id){
    if(!/^job-[0-9a-f]{32}$/.test(id))return;
    clearTimeout(pollTimer);rememberPending(id);intakeDraft=null;
    if(!dialog.open)dialog.showModal();
    return checkJob(id,{show:true});
  }

  async function resumePending(){
    const id=readPendingId();
    if(!id)return;
    pendingJobId=id;
    return checkJob(id);
  }

  function configure(options={}){
    if(typeof options.onComplete==='function')onComplete=options.onComplete;
    return window.ReviewJobsUI;
  }

  openButton?.addEventListener('click',open);
  window.addEventListener?.('api-profiles-changed',()=>refreshReadyConfig?.());
  window.ReviewJobsUI={configure,open,openJob,resumePending};
})();
