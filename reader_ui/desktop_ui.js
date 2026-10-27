'use strict';
// The ordinary browser has no native bridge and no software-removal entry.
window.DesktopUI=(()=>{
  const $=id=>document.getElementById(id);
  const section=$('desktop-settings'),dialog=$('desktop-remove-dialog');
  const launch=$('desktop-remove-open'),submit=$('desktop-remove-confirm');
  const erase=$('desktop-remove-data'),cancel=$('desktop-remove-cancel'),close=$('desktop-remove-close');
  let info=null,busy=false;
  function bridge(){return window.pywebview?.api;}
  function error(id,message){const e=$(id);e.textContent=message||'';e.hidden=!message;}
  function markBusy(value){busy=value;submit.disabled=value;erase.disabled=value;cancel.disabled=value;close.disabled=value;}
  function selection(){
    $('desktop-remove-data-note').textContent=erase.checked
      ?'这些数据会移到 Windows 回收站，需要时可以恢复。'
      :'审查记录、讨论和模型设置会保留在下面的位置，程序移除后仍可找到。';
    $('desktop-remove-data-path').textContent=info?.data_dir||'';
    submit.textContent=erase.checked?'移除软件和数据':'保留数据并移除软件';
  }
  // The saved path is the folder on this computer; clicking opens it in Explorer.
  function showDataLocation(api,folder){
    const host=$('desktop-data-location');host.replaceChildren('审查记录保存在：');
    if(!api.open_data_folder){host.append(folder);return;}
    const link=document.createElement('button');link.type='button';link.className='desktop-data-open';
    link.textContent=folder;link.title='在资源管理器中打开这个文件夹';
    link.onclick=async()=>{
      try{const result=await api.open_data_folder();error('desktop-settings-error',result?.ok?'':(result?.error||'未能打开文件夹。'));}
      catch(e){error('desktop-settings-error','未能打开文件夹，请稍后重试。');}
    };
    host.append(link);
  }
  async function refresh(){
    const api=bridge();
    if(!api?.get_desktop_info){section.hidden=true;return null;}
    try{
      const next=await api.get_desktop_info();
      if(!next?.available){section.hidden=true;return null;}
      info=next;section.hidden=false;launch.disabled=!info.remove_allowed;
      $('model-settings-title').textContent='设置';
      $('settings-close').setAttribute('aria-label','关闭设置');
      showDataLocation(api,info.data_dir);
      error('desktop-settings-error',info.remove_allowed?'':(info.reason||'当前版本不能从这里移除。'));
      return info;
    }catch(e){section.hidden=false;launch.disabled=true;error('desktop-settings-error','软件信息未能读取，请关闭设置后重试。');return null;}
  }
  async function open(){
    if(busy)return;
    const current=await refresh();if(!current?.remove_allowed)return;
    erase.checked=false;selection();error('desktop-remove-error','');
    $('desktop-remove-app-path').textContent=current.app_dir;
    dialog.showModal();cancel.focus();
  }
  function dismiss(){if(!busy)dialog.close();}
  launch.onclick=open;cancel.onclick=dismiss;close.onclick=dismiss;erase.onchange=selection;
  dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  dialog.addEventListener('click',event=>{if(event.target===dialog)dismiss();});
  submit.onclick=async()=>{
    if(busy||!info?.remove_allowed)return;
    const api=bridge();if(!api?.request_remove){error('desktop-remove-error','请在合同审查台的软件窗口中操作。');return;}
    markBusy(true);error('desktop-remove-error','');submit.textContent='正在准备退出…';
    try{
      const result=await api.request_remove(erase.checked);
      if(result?.cancelled){markBusy(false);selection();return;}
      if(!result?.ok)throw new Error(result?.error||'移除尚未开始，请稍后重试。');
      submit.textContent='正在退出并移除…';
    }catch(e){error('desktop-remove-error',e.message||'移除尚未开始，请稍后重试。');markBusy(false);selection();}
  };
  window.addEventListener('pywebviewready',refresh);
  $('workbench-settings').addEventListener('click',refresh);
  if(bridge())refresh();
  return {refresh,open};
})();
