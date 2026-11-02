const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const [base,assignment,exam,key,job,other,out,width,mode]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:1000}}),writes=[],errors=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')writes.push(new URL(r.url()).pathname)});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 if(mode==='lost')await page.route('**/api/jobs/'+job+'/delete',async r=>{const response=await r.fetch();assert.equal(response.status(),200);await r.abort()});
 try{
  const viewed=mode==='history'?other:job;
  await page.goto(base+'/next/#/objective?assignment='+assignment+'&exam='+exam+'&assessment='+key+'&job='+viewed);
  const frame=page.frameLocator('iframe');await frame.locator('#task-current').filter({hasText:viewed.slice(0,8)}).waitFor({state:'attached'});
  await frame.getByText('高级管理：本次任务与历史任务',{exact:true}).click();
  if(mode==='history'){await frame.getByText('查看最近任务（最多100组）',{exact:true}).click();await frame.locator('.delete-task[data-job="'+job+'"]').click()}
  else await frame.locator('#delete-current-task').click();
  const panel=frame.getByRole('region',{name:'删除客观题任务确认'}),submit=frame.locator('#job-delete-submit');
  await panel.locator('#job-delete-preview').filter({hasText:'答卷 2 份'}).waitFor();assert(await submit.isDisabled());assert.deepEqual(writes,[]);
  assert((await panel.innerText()).includes('主观题、其他任务'));assert((await panel.innerText()).includes('照片 2 张'));
  await panel.locator('#job-delete-consent').check();assert(await submit.isEnabled());await page.screenshot({path:out+'-preview.png',fullPage:true});await submit.click();
  if(mode==='lost'){
   await panel.locator('#job-delete-error').filter({hasText:'不会自动重试'}).waitFor();assert(await submit.isDisabled());await page.waitForTimeout(500);assert.equal(writes.length,1);
   await panel.locator('#job-delete-close').click();await frame.locator('#refresh-tasks').click();await frame.locator('#task-current').filter({hasText:'当前任务已删除'}).waitFor({state:'attached'});
  }else await frame.locator('#task-delete-message').filter({hasText:'已删除'}).waitFor();
  await frame.locator('.delete-task[data-job="'+job+'"]').waitFor({state:'detached'});
  await page.waitForFunction(({job,mode,other})=>mode==='history'?location.hash.includes('job='+other):!location.hash.includes('job='+job),{job,mode,other});
  await frame.locator('#results tbody > tr').filter({hasText:'SYNTHETIC-OTHER'}).waitFor();assert.equal(await frame.locator('#results tbody > tr').count(),1);
  if(mode==='warning')await frame.locator('#task-error').filter({hasText:'图片清理失败'}).waitFor();
  assert.deepEqual(writes,['/objective/api/jobs/'+job+'/delete']);assert.deepEqual(errors,[]);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:out+'-after.png',fullPage:true});console.log('PASS '+width+'px '+mode+': one explicit delete, task/URL/results refresh, other scores and source preserved, no model calls');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
