async page => {
  const passed=[];const assert=(ok,s)=>{if(!ok)throw new Error(s);passed.push(s);};
  await page.reload();await page.getByText('Bulk QA Match',{exact:true}).click();
  await page.getByRole('button',{name:'Bulk edit',exact:true}).focus();await page.keyboard.press('b');
  assert(await page.locator('#bulk-modal').evaluate(el=>el.classList.contains('active')),'B opens bulk dialog');
  assert(await page.locator('#tagging-screen').evaluate(el=>el.inert),'Background is inert');
  await page.keyboard.press('Shift+Tab');
  assert(await page.locator('#btn-bulk-cancel').evaluate(el=>el===document.activeElement),'Shift+Tab wraps first control to Cancel');
  await page.keyboard.press('Tab');
  assert(await page.locator('#bulk-tag-type').evaluate(el=>el===document.activeElement),'Tab wraps Cancel to first control');
  for(let i=0;i<8;i++){await page.keyboard.press('Tab');assert(await page.locator('#bulk-modal').evaluate(el=>el.contains(document.activeElement)&&!document.activeElement.disabled&&!document.activeElement.hidden),`Tab step ${i+1} stays on available dialog control`);}
  await page.keyboard.press('Escape');
  assert(await page.locator('#btn-bulk-edit').evaluate(el=>el===document.activeElement),'Escape restores Bulk edit button focus');
  assert(!(await page.locator('#tagging-screen').evaluate(el=>el.inert)),'Escape restores interactive background');
  await page.keyboard.press('Control+b');
  assert(!(await page.locator('#bulk-modal').evaluate(el=>el.classList.contains('active'))),'Modified B does not open dialog');
  await page.getByPlaceholder('Label (optional)').fill('b');
  assert(!(await page.locator('#bulk-modal').evaluate(el=>el.classList.contains('active'))),'Typing B does not open dialog');
  await page.getByRole('button',{name:'Players',exact:true}).click();await page.keyboard.press('b');
  assert(!(await page.locator('#bulk-modal').evaluate(el=>el.classList.contains('active'))),'B does not stack over another modal');
  await page.reload();await page.getByText('Bulk QA Match',{exact:true}).click();await page.getByRole('button',{name:'Bulk edit',exact:true}).click();
  await page.getByLabel('Tag type',{exact:true}).selectOption('Foul');await page.getByRole('button',{name:'Preview',exact:true}).click();await page.waitForFunction(()=>!document.getElementById('btn-bulk-confirm').disabled);
  let release,ready;const held=new Promise(r=>release=r),committed=new Promise(r=>ready=r);
  await page.route('**/clips/bulk_apply',async route=>{const response=await route.fetch();ready();await held;await route.fulfill({response});});
  try{
    await page.getByRole('button',{name:'Confirm',exact:true}).focus();await page.keyboard.press('Enter');await committed;
    assert(await page.getByRole('button',{name:'Confirm',exact:true}).isDisabled(),'Native Enter starts confirmation');
    assert(await page.locator('#bulk-modal').evaluate(el=>el.contains(document.activeElement)&&!document.activeElement.disabled&&!document.activeElement.hidden),'Disabling focused Confirm moves focus to available dialog control');
    await page.keyboard.press('Tab');assert(await page.locator('#bulk-modal').evaluate(el=>el.contains(document.activeElement)),'Tab stays inside during apply');
    const done=page.waitForResponse('**/clips/bulk_apply');release();await done;await page.locator('#bulk-status').filter({hasText:'updated'}).waitFor();
  }finally{release();await page.unroute('**/clips/bulk_apply');}
  return{passed};
}
