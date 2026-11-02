const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const [base,assignment,exam,key,job,out]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
 try{for(const width of [1280,390]){
  const page=await browser.newPage({viewport:{width,height:950}}),errors=[],writes=[];
  page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')writes.push(r.url())});
  await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  await page.goto(base+'/next/#/objective?assignment='+assignment+'&exam='+exam+'&assessment='+key+'&job='+job);
  const frame=page.frameLocator('iframe');
  const rows=frame.locator('#results tbody > tr');await rows.nth(6).waitFor();assert.equal(await rows.count(),7);
  assert((await frame.locator('#exam-summary').innerText()).includes('当前版本有效 6 份'));
  assert((await frame.locator('#objective-statistics').textContent()).includes('100.0%'));
  assert.equal(await frame.locator('#result-scope').inputValue(),'exam');
  assert.equal(await frame.getByText('高级管理：本次任务与历史任务',{exact:true}).evaluate(e=>e.parentElement.open),false);
  await frame.locator('#refresh-results').click();assert.equal(await rows.count(),7);
  await frame.getByText('高级管理：本次任务与历史任务',{exact:true}).click();await frame.locator('#result-scope').selectOption('job');
  await page.waitForFunction(()=>document.querySelector('iframe').contentDocument.querySelectorAll('#results tbody > tr').length===2);
  await frame.locator('#result-scope').selectOption('exam');await rows.nth(6).waitFor();
  await page.reload();await rows.nth(6).waitFor();assert.equal(await frame.locator('#result-scope').inputValue(),'exam');
  const response=page.waitForResponse(r=>r.url().includes('export.xlsx'));await frame.locator('#export-xlsx').click();assert.equal((await response).status(),200);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  assert(await frame.locator('body').evaluate(e=>e.scrollWidth<=innerWidth+1));
  await frame.locator('#results-panel').scrollIntoViewIfNeeded();await page.screenshot({path:out+'/results-'+width+'.png'});
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  assert.equal(await frame.locator('.reason-input').count(),0);
  const first=frame.locator('.review-card').first();await first.scrollIntoViewIfNeeded();
  await first.locator('select.option').evaluate(e=>e.value='');await first.locator('.save-review').click();assert.equal(writes.length,0);
  for(const option of ['A','__blank']){
   const count=Number(await frame.locator('#review-count').innerText());
   const card=frame.locator('.review-card').first();await card.locator('select.option').selectOption(option);
   const saved=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/review'));
   await card.locator('.save-review').click();const response=await saved;assert.equal(response.status(),200);
   assert.deepEqual(JSON.parse(response.request().postData()),{option:option==='__blank'?'':option});
   await page.waitForFunction(n=>Number(document.querySelector('iframe').contentDocument.querySelector('#review-count').textContent)===n,count-1);
  }
  assert.equal(writes.length,2);assert.deepEqual(errors,[]);await page.close();
  console.log('PASS '+width+'px: 3 jobs/6 valid+1 review, whole-exam default/reload, task advanced filter, correctness, Excel, reason-free answer/blank review, missing selection blocked, no model calls');
 }}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
