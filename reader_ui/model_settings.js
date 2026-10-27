'use strict';
window.ModelSettings=(()=>{
  const $=id=>document.getElementById(id);
  const dialog=$('model-settings-dialog'),form=$('model-settings-form');
  const provider=$('settings-provider'),base=$('settings-base-url'),secret=$('settings-api-key'),model=$('settings-model-id');
  const save=$('settings-save'),remove=$('settings-remove'),error=$('settings-error');
  const picker=window.ModelPicker.mount($('settings-model-field'),model,{base,key:secret,provider});
  let busy=false;
  function showError(message){error.textContent=message;error.hidden=!message;}
  function markBusy(value){busy=value;save.disabled=value;remove.disabled=value;provider.disabled=value;save.textContent=value?'保存中…':'保存设置';}
  function fill(){
    showError('');const value=APIProfiles.get(provider.value);
    base.value=value.base_url;secret.value=value.api_key;model.value=value.model;
    remove.hidden=!APIProfiles.hasSaved(provider.value);picker.reset();
  }
  async function open(){
    if(!dialog.open)dialog.showModal();showError('');save.disabled=true;
    try{
      const presets=await APIProfiles.load({reload:true});provider.replaceChildren();
      for(const p of presets)provider.add(new Option(p.label,p.id));
      provider.value=APIProfiles.active()?.provider||presets[0]?.id||'';fill();save.disabled=false;
    }catch(e){showError(e.message);}
  }
  form.onsubmit=async event=>{
    event.preventDefault();if(busy||!picker.validate())return;
    markBusy(true);showError('');
    try{
      await APIProfiles.save(provider.value,{base_url:base.value.trim(),api_key:secret.value.trim(),model:model.value.trim()});
      dialog.close();
    }catch(e){showError(e.message);}finally{markBusy(false);}
  };
  remove.onclick=async()=>{
    if(busy||!confirm('移除“'+provider.value+'”保存在本机的配置？下次使用需要重新填写密钥，合同与讨论记录不会删除。'))return;
    markBusy(true);showError('');
    try{await APIProfiles.remove(provider.value);fill();}
    catch(e){showError(e.message);}finally{markBusy(false);}
  };
  provider.onchange=fill;
  $('workbench-settings').onclick=open;
  $('settings-close').onclick=()=>{if(!busy)dialog.close();};
  dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  dialog.addEventListener('click',e=>{if(e.target===dialog&&!busy)dialog.close();});
  APIProfiles.load().catch(()=>{$('workbench-settings').title='模型设置暂未读取，点击重试';});
  return {open};
})();
