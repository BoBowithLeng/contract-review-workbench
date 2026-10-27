'use strict';
// Presentation only. Stored model replies and their source IDs stay unchanged.
window.ReaderText=(()=>{
  const inline=value=>String(value??'').replace(/\s+/g,' ').trim().replace(/[\\`*_{}\[\]<>|#]/g,'\\$&');
  const escaped=value=>value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  // Page titles from web search end with the site's name (“… - ARTICLE”, “… | 发现原创”).
  function webTitle(title){
    let value=String(title||'').trim(),previous;
    do{previous=value;value=value.replace(/(?:\s[-_]\s|\s?[|｜]\s?)[^-_|｜]{1,15}$/,'').trim();}while(value&&value!==previous);
    return value||String(title||'').trim();
  }
  function compactLocation(location){
    const quoted=/ · “([^”]+)”$/.exec(location);
    if(!quoted)return location;
    const excerpt=quoted[1].replace(/^[（(][一二三四五六七八九十\d]+[）)]\s*/,'');
    const shortened=excerpt.length>18;
    return '原文“'+excerpt.slice(0,18)+(shortened?'…':'')+'”';
  }
  function shortReference(paragraph={}){
    const location=String(paragraph.location||''),text=String(paragraph.display_text||paragraph.text||'').trim();
    const parts=location.split(/\s*(?:\/| · )\s*/);
    const attachment=parts.some(part=>/^附件/.test(part))?'附件':'';
    const own=String(paragraph.numbering_label||'').trim()||text.match(/^(第[一二三四五六七八九十百零〇\d]+条|\d+(?:\.\d+)*)(?=[\s、.．:：]|$)/)?.[1];
    const pathNumber=[...parts].reverse().map(part=>part.match(/^(第[一二三四五六七八九十百零〇\d]+条|\d+(?:\.\d+)*)(?=[\s、.．:：]|$)/)?.[1]).find(Boolean);
    const number=String(own||pathNumber||'').replace(/[.．、]+$/,'');
    if(number)return attachment+(number.startsWith('第')?number:'第'+number+'条');
    const table=/表格\d+·第\d+行第\d+列/.exec(location);
    if(table)return attachment+table[0];
    const excerpt=text.replace(/^[（(][一二三四五六七八九十\d]+[）)]\s*/,'');
    if(excerpt)return attachment+'原文“'+excerpt.slice(0,14)+(excerpt.length>14?'…':'')+'”';
    return compactLocation(location)||'对应原文';
  }
  function locationParts(paragraph={}){
    const parts=String(paragraph.location||'').split(/\s*(?:\/| · )\s*/).filter(Boolean);
    const leaf=parts.at(-1)||shortReference(paragraph),parent=parts.slice(0,-1).join(' / ');
    const number=/^\d+(?:\.\d+)*\.?$/.test(leaf)?'第'+leaf.replace(/\.$/,'')+'条':leaf;
    const text=String(paragraph.text||paragraph.display_text||'').replace(/\s+/g,' ').trim();
    return {title:number,context:parent,excerpt:text.slice(0,52)+(text.length>52?'…':'')};
  }
  // "（10）" items sit in the location path; they do not make a range span two different places.
  const scopeKey=d=>String(d.scope||'').split(/\s*\/\s*/).filter(part=>!/^[（(][0-9一二三四五六七八九十]+[）)]$/.test(part)).join('/');
  const sameScope=(a,b)=>scopeKey(a)===scopeKey(b);
  const pathParts=d=>String(d.location||'').split(/\s*(?:\/| · )\s*/).filter(Boolean);
  const nestedIn=(inner,outer)=>{const o=pathParts(outer),i=pathParts(inner);return o.length>0&&i.length>o.length&&o.every(part=>i.includes(part));};
  // Label tables and the combined pattern depend only on the context, not on the text. A whole report
  // has hundreds of paragraphs, so rebuilding them per call made each click redo the same work dozens
  // of times. Callers opt in by freezing context.locations; unfrozen contexts are rebuilt every call.
  const preparedCache=new WeakMap();
  function prepare(context,options){
    const mode=options.shortLocations?'short':options.fullLocations?'full':'compact';
    const anchor=context.locations&&typeof context.locations==='object'&&Object.isFrozen(context.locations)?context.locations:null;
    const cached=anchor&&preparedCache.get(anchor)?.get(mode);
    if(cached&&cached.paragraphsRef===context.paragraphs&&cached.inlineRef===context.inline_locations&&cached.findingRef===context.finding_labels
      &&cached.optionRef===context.option_labels&&cached.sourceRef===context.sources)return cached;
    const labels=new Map(),details=new Map(),findingKeys=new Map();
    const setLabel=(key,label,findingId)=>{labels.set(key,label);if(findingId===undefined)findingKeys.delete(key);else findingKeys.set(key,findingId);};
    const paragraphs=new Map((context.paragraphs||[]).map(p=>[p.id,p]));
    const entries=Object.entries(context.locations||{}).filter(([,location])=>typeof location==='string'&&location.trim());
    const positions=entries.map(([id,location])=>{
      const paragraph=paragraphs.get(id)||{location},parts=locationParts(paragraph);
      let base=context.inline_locations?.[id]||shortReference(paragraph);
      const attachment=location.split(/\s*(?:\/| · )\s*/).find(part=>/^附件/.test(part));
      if(attachment&&attachment!=='附件'&&base.startsWith('附件'))base=attachment+'·'+base.slice(2);
      const raw=String(paragraph.text||paragraph.display_text||/“([^”]+)”/.exec(location)?.[1]||'').replace(/\s+/g,' ').trim();
      const excerpt=raw.slice(0,24)+(raw.length>24?'…':'');
      return {id,location,base,scope:parts.context,excerpt};
    });
    const positionsById=new Map(positions.map(p=>[p.id,p])),counts=new Map();for(const p of positions)counts.set(p.base,(counts.get(p.base)||0)+1);
    for(const [id,location] of Object.entries(context.locations||{})){
      if(typeof location!=='string'||!location.trim())continue;
      const detail=positionsById.get(id),duplicate=counts.get(detail.base)>1;
      const label=options.shortLocations?detail.base+(duplicate&&detail.excerpt?' · 原文“'+detail.excerpt+'”':''):options.fullLocations?location:compactLocation(location);
      setLabel(id,label);
      details.set(id,detail);
      const piece=/^P0*(\d+)$/.exec(id);
      if(piece){setLabel('P'+Number(piece[1]),label);details.set('P'+Number(piece[1]),detail);}
    }
    // The current finding reads “本条意见”; that is decided per call in labelFor(), so one table serves every finding.
    for(const [id,label] of Object.entries(context.finding_labels||{})){
      setLabel(id,label,id);
      const finding=/^F0*(\d+)$/.exec(id);
      if(finding)setLabel('F'+Number(finding[1]),label,id);
    }
    for(const [id,label] of Object.entries(context.option_labels||{})){
      setLabel(id,label);const match=/^O0*(\d+)$/.exec(id);if(match)setLabel('O'+Number(match[1]),label);
    }
    for(const source of context.sources||[]){
      if(!source?.id||!source.title)continue;
      // Web search results carry page titles with site suffixes (“- ARTICLE”, “| 发现原创”); keep the article name only.
      const cleaned=/^WEB/.test(source.id)?webTitle(source.title):source.title;
      const title=/^WEB/.test(source.id)&&cleaned&&!/^《/.test(cleaned)?'《'+cleaned+'》':cleaned;
      setLabel(source.id,[title,source.article].filter(Boolean).join(' · '));
      const numeral=/劳动争议.*解释[（(]([一二三四五六七八九十]+)[）)]/.exec(source.title);
      if(numeral)setLabel('SPC解释'+numeral[1],source.title);
    }
    for(const [key,label] of Object.entries({current_text:'当前条款文字',original_text:'原合同文字',
      clause_draft:'建议改稿',draft_plan:'建议改稿',research_memo:'检索记录',source_ids:'引用依据',finding_id:'审查意见',rule_id:'审查主题',
      confirmed_edit_ids:'已确认的合同修改',finding:'审阅意见',findings:'审阅意见'}))setLabel(key,label);
    const keys=[...labels.keys()].sort((a,b)=>b.length-a.length).map(escaped);
    const pattern=new RegExp('(?<![A-Za-z0-9_])('+keys.join('|')+')(?![A-Za-z0-9_])','g');
    const range=/(?<![A-Za-z0-9_])((?:P\d+|T\d+R\d+C\d+P\d+))\s*([-—–~～至])\s*((?:P\d+|T\d+R\d+C\d+P\d+))(?![A-Za-z0-9_])/g;
    const referencePattern=new RegExp(range.source+'|'+pattern.source,'g');
    const result={labels,details,findingKeys,pattern,referencePattern,paragraphsRef:context.paragraphs,inlineRef:context.inline_locations,
      findingRef:context.finding_labels,optionRef:context.option_labels,sourceRef:context.sources};
    if(anchor){let byMode=preparedCache.get(anchor);if(!byMode){byMode=new Map();preparedCache.set(anchor,byMode);}byMode.set(mode,result);}
    return result;
  }
  function humanize(value,context={},options={}){
    const text=String(value??'');
    const {labels,details,findingKeys,pattern,referencePattern}=prepare(context,options);
    if(!labels.size)return text;
    const current=context.current_finding_id;
    const labelFor=key=>current!==undefined&&current!==null&&findingKeys.get(key)===current?'本条意见':labels.get(key);
    // Keep code, Markdown link destinations and bare URLs literal.
    const protectedParts=/(```[^\n]*\n[\s\S]*?(?:```|$)|~~~[^\n]*\n[\s\S]*?(?:~~~|$)|`[^`\n]+`|\]\([^\n)]*\)|https?:\/\/[^\s<>\])]+)/g;
    const present=part=>part
      .replace(/(?<![A-Za-z0-9_])confirmed_edit_ids\s*(?:为|是|=|:|：)?\s*(?:空|\[\s*\])/g,'尚未确认修改')
      .replace(referencePattern,(id,first,separator,last,key,offset,whole)=>{
        let label;
        if(first){
          if(!labels.has(first)||!labels.has(last))return id.replace(pattern,key=>options.plain?labelFor(key):inline(labelFor(key)));
          const a=details.get(first),b=details.get(last);
          if(options.shortLocations&&a&&b&&a.base===b.base&&sameScope(a,b)){
            label=a.id===b.id?a.base:a.excerpt&&b.excerpt?a.base+'，原文“'+a.excerpt+'”至“'+b.excerpt+'”':a.base+'的相关段落';
          }else if(options.shortLocations&&a&&b&&nestedIn(b,a)){
            // A clause heading through its own numbered items (“9.2 … / 7.”) is that one clause, not two places.
            label=a.excerpt&&b.excerpt?a.base+'，原文“'+a.excerpt+'”至“'+b.excerpt+'”':a.base;
          }else{
            const endpoint=(detail,key,other)=>options.shortLocations&&detail&&other&&detail.base===other.base&&!sameScope(detail,other)?(detail.scope?detail.scope+'·':'')+labelFor(key):labelFor(key);
            label=endpoint(a,first,b)+'—'+endpoint(b,last,a);
          }
        }else label=labelFor(id);
        if(options.shortLocations&&label.startsWith('附件')&&/附件\s*$/.test(whole.slice(0,offset)))label=label.slice(2);
        return options.plain?label:inline(label);
      })
      .replace(options.hideUnknown?/(?<![A-Za-z0-9_])(?:P\d+|T\d+R\d+C\d+P\d+|F\d+)(?![A-Za-z0-9_])/g:/(?!)/g,id=>id.startsWith('F')?'相关意见':'对应原文');
    // Two source IDs can resolve to the same article; list it once (“A、A” → “A”).
    return text.split(protectedParts).map((part,index)=>index%2?(options.plain&&/^`[^`\n]+`$/.test(part)?present(part.slice(1,-1)):part):present(part)).join('')
      .replace(/([^、（()）\n]{4,})(?:、\1)+(?=[）)、])/g,'$1');
  }
  function findingTitle(finding={},report={}){
    if(finding.status!=='处理失败')return finding.title||'未命名意见';
    const title=String(finding.title||'').trim();
    if(title&&!/模型.*(?:结果|校验)|(?:格式|字段|枚举).*错误|^审阅意见校验失败$/.test(title))return title;
    const checks=report.preparation?.checks||report.checks||[];
    const check=checks.find(check=>check.id===finding.rule_id)||checks.find(check=>(report.checklist_results||[]).some(row=>row.check_id===check.id&&row.finding_ids?.includes(finding.id)));
    return check?.title||'本项检查尚未完成';
  }
  function sourceNote(source={},programDerived=false){
    const candidate=source.verification_state==='candidate'||/^WEB/.test(source.id||'');
    const checked=programDerived&&source.snapshot_verified===true&&source.verification_state==='verified_snapshot';
    if(!programDerived)return candidate?'（联网检索结果，原文待核对）':'（原文核对情况未记录）';
    const status=candidate?'联网检索结果，原文待核对':checked?'已核对原文':'原文待核对';
    // Keep the frozen registry date; the report date is not a new source check.
    const date=/^\d{4}-\d{2}-\d{2}(?:$|T| )/.exec(String(source.checked_at||''))?.[0].slice(0,10);
    return '（'+status+(checked&&date?' · 核对日期 '+date:'')+'）';
  }
  // An option may point at the real drafts rather than contain a usable clause itself.
  function resolveDrafts(option,findings,reviews={}){
    const value=String(option.clause_draft||'');
    const referenceOnly=/^\s*(?:见|参见|参考|详见|请参(?:考|见)|可参(?:考|见)|同|采用|使用|建议(?:采用|使用))[^\n。]*\b(?:clause_draft|finding(?:s)?|F\d+)\b[^\n]*(?:[。.]|$)\s*$/i.test(value)&&!/[“”「」]/.test(value);
    if(!referenceOnly)return {referenceOnly:false,drafts:value?[{title:'',text:value}]:[]};
    const drafts=[],missing=[],identity=id=>/^F\d+$/.test(id)?'F'+Number(id.slice(1)):id;
    const linked=new Set((option.finding_ids||[]).map(identity));
    const explicit=[...value.matchAll(/(?<![A-Za-z0-9_])F\d+(?![A-Za-z0-9_])/g)].map(match=>match[0]);
    const requested=[...new Map((explicit.length?explicit:option.finding_ids||[]).map(id=>[identity(id),id])).values()];
    for(const reference of requested){
      const f=linked.has(identity(reference))?findings.find(item=>identity(item.id)===identity(reference)):null;
      const review=f&&reviews[f.id];const text=f&&(review?.decision==='修改'&&(!f.opinion_revision||review.opinion_revision===f.opinion_revision)?review.clause_draft:f.clause_draft);
      if(text&&f.status!=='处理失败')drafts.push({id:f.id,title:f.title,text});else missing.push(reference);
    }
    return {referenceOnly:true,drafts,missing};
  }
  return {humanize,shortReference,locationParts,resolveDrafts,sourceNote,findingTitle};
})();
