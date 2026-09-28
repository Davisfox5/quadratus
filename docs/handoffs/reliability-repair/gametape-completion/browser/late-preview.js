async page => {
  await page.reload();
  await page.getByText('Bulk QA Match',{exact:true}).click();
  await page.getByLabel('Filter by tag type').selectOption('Goal');
  let release;
  const delayed = new Promise(resolve => release = resolve);
  await page.route('**/clips/bulk_preview', async route => { await delayed; await route.continue(); });
  await page.getByRole('button', {name:'Bulk edit', exact:true}).click();
  await page.getByLabel('Tag type', {exact:true}).selectOption('Pass');
  await page.getByRole('button', {name:'Preview', exact:true}).click();
  await page.getByRole('status').filter({hasText:'Previewing'}).waitFor();
  await page.getByRole('button', {name:'Cancel (Esc)'}).click();
  await page.getByLabel('Filter by tag type').selectOption('Shot');
  await page.getByRole('button', {name:'Bulk edit', exact:true}).click();
  const response = page.waitForResponse('**/clips/bulk_preview');
  release();
  await response;
  await page.waitForFunction(() => !document.getElementById('btn-bulk-preview').disabled);
  const result = {
    confirmDisabled: await page.getByRole('button', {name:'Confirm',exact:true}).isDisabled(),
    rows: await page.locator('#bulk-preview-body tr').count(),
    heading: await page.getByRole('heading', {name:/Bulk edit/}).innerText()
  };
  await page.unroute('**/clips/bulk_preview');
  if (!result.confirmDisabled || result.rows !== 0 || !result.heading.includes('1 filtered')) throw new Error('Late preview restored stale state');
  return result;
}
