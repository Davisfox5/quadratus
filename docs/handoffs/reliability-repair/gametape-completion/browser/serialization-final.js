async page => {
  const passed=[];
  const assert=(ok,label)=>{if(!ok)throw new Error(label);passed.push(label);};
  const projects=await (await page.request.get('http://127.0.0.1:5088/api/projects')).json();
  const a=projects.find(p=>p.name==='Bulk QA Match'),b=projects.find(p=>p.name==='Other QA Match');
  for(const p of [a,b])for(const c of p.clips.slice(0,2))await page.request.put(`http://127.0.0.1:5088/api/projects/${p.id}/clips/${c.id}`,{data:{tag_type:'Goal'}});
  let release,ready,requests=0;
  const held=new Promise(r=>release=r),committed=new Promise(r=>ready=r);
  await page.route('**/clips/bulk_apply',async route=>{requests++;const response=await route.fetch();if(requests===1){ready();await held;}await route.fulfill({response});});
  async function open(p,filter='Goal'){await page.getByText(p.name,{exact:true}).click();await page.getByLabel('Filter by tag type').selectOption(filter);await page.getByRole('button',{name:'Bulk edit',exact:true}).click();}
  async function back(){await page.getByRole('button',{name:'Cancel (Esc)'}).click();await page.getByRole('button',{name:'← Projects'}).click();}
  async function preview(tag){await page.getByLabel('Tag type',{exact:true}).selectOption(tag);await page.getByRole('button',{name:'Preview',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('btn-bulk-confirm').disabled);}
  try{
    await page.reload();await open(a);await preview('Pass');await page.getByRole('button',{name:'Confirm',exact:true}).click();await committed;
    await page.getByLabel('Tag type',{exact:true}).selectOption('Shot');
    assert(await page.getByRole('button',{name:'Preview',exact:true}).isDisabled(),'Selection change cannot enable overlapping preview');
    assert(await page.getByRole('button',{name:'Confirm',exact:true}).isDisabled(),'Second confirmation unavailable while first response held');
    await page.getByRole('button',{name:'Cancel (Esc)'}).click();await page.getByRole('button',{name:'Bulk edit',exact:true}).click();
    assert(await page.getByRole('button',{name:'Preview',exact:true}).isDisabled(),'Dismiss and reopen preserves pending guard');
    await back();await open(b);await preview('Pass');
    assert(await page.getByRole('button',{name:'Confirm',exact:true}).isEnabled(),'Other project can prepare a preview');
    await page.getByRole('button',{name:'Confirm',exact:true}).click();
    assert(requests===1,'Global serialization refuses second project apply without losing first marker');
    assert((await page.locator('#bulk-status').innerText()).includes('Applying'),'Blocked second project has visible busy explanation');
    await back();await open(a,'Pass');
    assert(await page.getByRole('button',{name:'Preview',exact:true}).isDisabled(),'Returning to first project retains its pending state');
    const done=page.waitForResponse(r=>r.url().includes(`/projects/${a.id}/clips/bulk_apply`));release();await done;
    await page.waitForFunction(()=>!document.getElementById('btn-bulk-preview').disabled);
    assert(await page.locator('#clips-list .clip-card').count()===2,'Dismissed and reopened committed result reconciles');
    await preview('Shot');await page.getByRole('button',{name:'Confirm',exact:true}).click();await page.locator('#bulk-status').filter({hasText:'2 clip(s) updated'}).waitFor();
    await page.getByRole('button',{name:'Cancel (Esc)'}).click();await page.getByLabel('Filter by tag type').selectOption('Shot');
    const saved=await (await page.request.get(`http://127.0.0.1:5088/api/projects/${a.id}`)).json();
    assert(await page.locator('#clips-list .clip-card').count()===3&&saved.clips.filter(c=>c.tag_type==='Shot').length===3,'Sequential second batch retains all three Shot clips in UI and server');
    await page.getByRole('button',{name:'← Projects'}).click();await open(b);await preview('Pass');await page.getByRole('button',{name:'Confirm',exact:true}).click();await page.locator('#bulk-status').filter({hasText:'2 clip(s) updated'}).waitFor();
    assert(requests===3,'Second project can apply after the pending request finishes');
    return{passed};
  }finally{release();await page.unroute('**/clips/bulk_apply');}
}
