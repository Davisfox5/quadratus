async page => {
  await page.reload();
  await page.getByText('Bulk QA Match',{exact:true}).click();
  await page.getByLabel('Filter by tag type').selectOption('Goal');
  await page.getByRole('button',{name:'Bulk edit',exact:true}).click();
  await page.getByLabel('Tag type',{exact:true}).selectOption('Pass');
  let release;
  const delayed=new Promise(resolve=>release=resolve);
  await page.route('**/clips/bulk_preview', async route=>{await delayed;await route.continue();});
  await page.getByRole('button',{name:'Preview',exact:true}).click();
  await page.locator('#bulk-status').filter({hasText:'Previewing'}).waitFor();
  await page.getByRole('button',{name:'Cancel (Esc)'}).click();
  await page.getByRole('button',{name:'← Projects'}).click();
  await page.getByText('Other QA Match',{exact:true}).click();
  await page.getByRole('button',{name:'Bulk edit',exact:true}).click();
  const response=page.waitForResponse('**/clips/bulk_preview');release();await response;
  await page.waitForFunction(()=>!document.getElementById('btn-bulk-preview').disabled);
  const result={
    project:await page.getByRole('heading',{level:2}).innerText(),
    heading:await page.getByRole('heading',{name:/Bulk edit/}).innerText(),
    rows:await page.locator('#bulk-preview-body tr').count(),
    confirmDisabled:await page.getByRole('button',{name:'Confirm',exact:true}).isDisabled()
  };
  await page.unroute('**/clips/bulk_preview');
  if(result.project!=='Other QA Match'||!result.heading.includes('3 filtered')||result.rows!==0||!result.confirmDisabled) throw new Error(JSON.stringify(result));
  return result;
}
