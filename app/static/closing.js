const closeBtn=document.querySelector('#close-btn');
const stateLabel={pending_transfer:'待转在制',closed:'已结零',finished_goods:'完工待入库',raw_stock:'原料库存',abnormal:'真实异常',signed:'已签字',waived:'异常放行',unsigned:'未签字'};
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function post(url){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'}});const d=await r.json();if(!r.ok)throw new Error(d.message||'操作失败');return d;}
function renderClosing(item){
  const snap=item.snapshot;const sum=snap.summary;
  const rows=snap.accounts.map(a=>`<tr class="${a.state==='abnormal'?'issue':''}"><td>${esc(a.lot)}</td><td>${esc(stateLabel[a.state]||a.state)}</td><td class="num">${esc(a.used_kg)}</td><td class="num">${esc(a.balance_kg)}</td><td class="num">${esc(a.pending_transfer_kg)}</td><td class="num">${esc(a.shortage_kg)}</td><td title="${esc(a.basis)}">${esc(a.basis)}</td></tr>`).join('');
  const signed=item.sign_state&&item.sign_state!=='unsigned';
  return `<div class="close-head"><b>第 ${item.closing_seq} 版结账</b><span>${signed?esc(stateLabel[item.sign_state]||item.sign_state):'未签字'} · 签字版 v${item.signed_version}</span>${item.reused?'<em>内容未变，复用旧版未新增记录</em>':''}${item.restored_from?`<em>已恢复自第 ${item.restored_from} 版</em>`:''}</div>
  <dl><dt>待转在制批次/量</dt><dd>${sum.pending_lots} 批 / ${esc(sum.pending_transfer_kg)} kg</dd><dt>异常批次/短缺</dt><dd class="${sum.shift_clean?'':'bad'}">${sum.abnormal_lots} 批 / ${esc(sum.shortage_kg)} kg</dd><dt>累计已用</dt><dd>${esc(sum.used_kg)} kg</dd><dt>结存合计</dt><dd>${esc(sum.balance_kg)} kg</dd></dl>
  <div class="table-scroll"><table class="ledger"><thead><tr><th>批次</th><th>判定</th><th class="num">已用</th><th class="num">结存</th><th class="num">待转</th><th class="num">短缺</th><th>数量依据</th></tr></thead><tbody>${rows}</tbody></table></div>
  <form id="sign-form" class="sign-form"><input type="hidden" name="signed_version" value="${item.signed_version}"><label class="field">签字<select name="state"><option value="signed">签字确认</option><option value="waived">异常放行</option></select></label><label class="field">复核依据<textarea name="note" rows="2" maxlength="500" placeholder="不少于4字">${esc(item.sign_note||'')}</textarea></label><button class="btn primary">提交签字</button><button type="button" class="btn" id="restore-btn">恢复上一版可签字结果</button></form>`;
}
if(closeBtn)closeBtn.addEventListener('click',async()=>{
  const box=document.querySelector('#close-result');box.textContent='结账中…';
  try{
    const item=await post('/api/reconcile/runs/'+traceState.run+'/close');
    box.innerHTML=renderClosing(item);
    bindForm(item.closing_seq);
  }catch(e){box.textContent='';inform(e.message,true);}
});
function bindForm(seq){
  const form=document.querySelector('#sign-form');
  if(form)form.addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(form);
    try{const item=await fetch('/api/reconcile/runs/'+traceState.run+'/closings/'+seq+'/sign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({signed_version:Number(f.get('signed_version')),state:f.get('state'),note:f.get('note')})}).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.message);return d;});
      document.querySelector('#close-result').innerHTML=renderClosing(item);bindForm(item.closing_seq);
    }catch(err){inform(err.message,true);}});
  const rb=document.querySelector('#restore-btn');
  if(rb){
    if(seq<=1){rb.disabled=true;rb.title='已是首版，无可恢复的上一版';}
    rb.addEventListener('click',async()=>{if(seq<=1)return;try{const item=await post('/api/reconcile/runs/'+traceState.run+'/closings/'+(seq-1)+'/restore');
    document.querySelector('#close-result').innerHTML=renderClosing(item);bindForm(item.closing_seq);}catch(e){inform(e.message,true);}});
  }
}
// 进入页面若已有结账，自动展示最新一版
if(closeBtn)fetch('/api/reconcile/runs/'+traceState.run+'/closings').then(r=>r.json()).then(d=>{
  if(d.items&&d.items.length){const item=d.items[d.items.length-1];document.querySelector('#close-result').innerHTML=renderClosing(item);bindForm(item.closing_seq);}
}).catch(()=>{});
