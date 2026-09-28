async page => {
  await page.reload();await page.getByText('Bulk QA Match',{exact:true}).click();await page.getByRole('button',{name:'Bulk edit',exact:true}).click();await page.getByLabel('Tag type',{exact:true}).selectOption('Corner');
  let release,ready;const held=new Promise(r=>release=r),started=new Promise(r=>ready=r);
  await page.route('**/clips/bulk_preview',async route=>{ready();await held;await route.continue();});
  try{
    await page.getByRole('button',{name:'Preview',exact:true}).focus();await page.keyboard.press('Enter');await started;
    const focus=await page.locator('#bulk-modal').evaluate(el=>({inside:el.contains(document.activeElement),id:document.activeElement.id,tag:document.activeElement.tagName,disabled:document.activeElement.disabled||false}));
    const done=page.waitForResponse('**/clips/bulk_preview');release();await done;
    if(!focus.inside||focus.disabled)throw new Error(JSON.stringify(focus));
    return{scenario:'Keyboard Enter on Preview while HTTP request is held',focus};
  }finally{release();await page.unroute('**/clips/bulk_preview');}
}
