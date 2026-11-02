const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const [base,assignment,exam,key,job]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
 try{for(const width of [1280,390]){
  const page=await browser.newPage({viewport:{width,height:950}}),errors=[],writes=[];
  page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')writes.push(r.url())});
  await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  await page.goto(base+'/next/#/objective?assignment='+assignment+'&exam='+exam+'&assessment='+key+'&job='+job);
  const frame=page.frameLocator('iframe');await frame.locator('#task-current').filter({hasText:job.slice(0,8)}).waitFor({state:'attached'});
  assert((await page.evaluate(()=>location.hash)).includes('job='+job));
  await page.reload();await frame.locator('#task-current').filter({hasText:job.slice(0,8)}).waitFor({state:'attached'});
  assert((await page.locator('iframe').getAttribute('src')).includes('job_id='+job));
  await frame.getByText('高级管理：本次任务与历史任务',{exact:true}).click();await frame.locator('#refresh-tasks').click();await frame.locator('#task-current').filter({hasText:job.slice(0,8)}).waitFor({state:'attached'});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);await page.close();
  console.log('PASS '+width+'px actual Vue/iframe task restore and refresh, no POST/provider calls');
 }}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
