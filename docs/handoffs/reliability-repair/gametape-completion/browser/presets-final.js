async page => {
  const projects=await (await page.request.get('http://127.0.0.1:5088/api/projects')).json();
  const p=projects.find(p=>p.name==='Bulk QA Match');
  for(let i=0;i<p.clips.length;i++)await page.request.put(`http://127.0.0.1:5088/api/projects/${p.id}/clips/${p.clips[i].id}`,{data:{tag_type:i<2?'Pass':'Shot'}});
  await page.reload();await page.getByText(p.name,{exact:true}).click();
  await page.getByLabel('Saved filter presets').selectOption({label:'QA Pass'});
  const passCount=await page.locator('#clips-list .clip-card').count();
  await page.getByRole('button',{name:'Bulk edit',exact:true}).click();await page.getByLabel('Tag type',{exact:true}).selectOption('Goal');await page.getByRole('button',{name:'Preview',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('btn-bulk-confirm').disabled);
  await page.getByRole('button',{name:'Cancel (Esc)'}).click();await page.getByLabel('Saved filter presets').selectOption({label:'QA Goal'});await page.getByRole('button',{name:'Bulk edit',exact:true}).click();
  const result={passPresetCount:passCount,goalPresetCount:await page.locator('#clips-list .clip-card').count(),previewRows:await page.locator('#bulk-preview-body tr').count(),confirmDisabled:await page.getByRole('button',{name:'Confirm',exact:true}).isDisabled()};
  if(result.passPresetCount!==2||result.goalPresetCount!==0||result.previewRows!==0||!result.confirmDisabled)throw new Error(JSON.stringify(result));
  return result;
}
