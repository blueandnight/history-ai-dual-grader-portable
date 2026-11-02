const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const [base,manifest,out,width,mode]=process.argv.slice(2),m=JSON.parse(fs.readFileSync(manifest)),writes=[],errors=[];
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:1000}});
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')writes.push(new URL(r.url()).pathname)});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 if(mode==='lost')await page.route('**/api/jobs/batch-delete',async r=>{const response=await r.fetch();assert.equal(response.status(),200);await r.abort()});
 try{
  await page.goto(base+'/next/#/objective?assignment='+m.assignment+'&exam='+m.exam+'&assessment='+m.key+'&job='+m.items[0].job_id);
  const frame=page.frameLocator('iframe');await frame.locator('#task-current').filter({hasText:m.items[0].job_id.slice(0,8)}).waitFor({state:'attached'});
  await frame.getByText('高级管理：本次任务与历史任务',{exact:true}).click();
  await frame.getByText('查看最近任务（最多100组）',{exact:true}).click();
  const active=frame.locator('.select-task[data-job="'+m.active_job+'"]');await active.waitFor();assert(await active.isDisabled());
  await frame.locator('#task-select-ended').click();await frame.locator('#task-selection-count').filter({hasText:'已选择20个任务'}).waitFor();await frame.locator('#task-select-clear').click();
  for(const item of m.items)await frame.locator('.select-task[data-job="'+item.job_id+'"]').check();
  await frame.locator('#jobs-delete-open').click();
  const preview=frame.locator('#jobs-delete-panel');await preview.locator('#jobs-delete-preview').filter({hasText:'所选 10 个任务'}).waitFor();
  assert((await preview.innerText()).includes('答卷 11 份'));assert(await frame.locator('#jobs-delete-submit').isDisabled());
  assert.deepEqual(writes,['/objective/api/jobs/batch-delete-preview']);await frame.locator('#jobs-delete-consent').check();
  await preview.screenshot({path:out+'-preview.png'});await frame.locator('#jobs-delete-submit').click();
  if(mode==='lost'){
   await frame.locator('#jobs-delete-error').filter({hasText:'不会自动重试'}).waitFor();assert(await frame.locator('#jobs-delete-submit').isDisabled());await page.waitForTimeout(300);
   assert.deepEqual(writes,['/objective/api/jobs/batch-delete-preview','/objective/api/jobs/batch-delete']);await frame.locator('#jobs-delete-close').click();await frame.locator('#refresh-tasks').click();
  }else await frame.locator('#task-delete-message').filter({hasText:'已批量删除 10 个'}).waitFor();
  await frame.locator('.select-task[data-job="'+m.items[0].job_id+'"]').waitFor({state:'detached'});
  assert.equal(await frame.locator('.select-task').count(),11);assert(await frame.locator('.select-task[data-job="'+m.active_job+'"]').isDisabled());
  await page.waitForFunction(jid=>!location.hash.includes('job='+jid),m.items[0].job_id);
  await frame.locator('#exam-summary').filter({hasText:'当前版本有效 9 份'}).waitFor();
  const data=await page.evaluate(async eid=>(await fetch('/objective/api/unified-scores?exam_id='+eid)).json(),m.exam);assert(!data.rows.some(r=>r.link_id));
  assert.deepEqual(writes,['/objective/api/jobs/batch-delete-preview','/objective/api/jobs/batch-delete']);assert.deepEqual(errors,[]);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));await frame.locator('#tasks-panel').screenshot({path:out+'-after.png'});
  console.log('PASS '+width+'px '+mode+': 10-task atomic deletion, live job disabled, one preview/one delete, score overview refreshed, no real models');
 }catch(e){fs.writeFileSync(out+'-failure.txt',await page.locator('body').innerText());await page.screenshot({path:out+'-failure.png',fullPage:true});throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
