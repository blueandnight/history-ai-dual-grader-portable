// Synthetic browser-only task history/refresh/ownership; every external request blocked.
const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..'),key='a'.repeat(32),job='b'.repeat(32),other='c'.repeat(32);
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
 try{for(const width of [1280,390]){
  const page=await browser.newPage({viewport:{width,height:1000}}),writes=[],errors=[];let completed=false;
  page.on('pageerror',e=>errors.push(String(e)));
  const summary=id=>({job_id:id,assessment_id:key,status:completed?'completed':'processing',total_submissions:1,processed_submissions:completed?1:0,complete_submissions:completed?1:0,review_submissions:0,error_submissions:0,total_files:1,grouping:'one_per_image',created_at:'2026-01-01T00:00:00+00:00',answer_version:1});
  await page.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());if(u.origin!=='http://objective.synthetic')return route.abort();if(req.method()==='POST')writes.push(u.pathname);
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'objective_static/index.html'),'utf8').replaceAll('__OBJECTIVE_BASE__','')});
   if(u.pathname.startsWith('/static/'))return route.fulfill({contentType:u.pathname.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(path.join(root,'objective_static',path.basename(u.pathname)),'utf8')});
   let data;
   if(u.pathname==='/api/auth/session')data={authenticated:true,csrf_token:'synthetic'};
   else if(u.pathname==='/api/status')data={vision_grading:{enabled:true,available:true,models:[]}};
   else if(u.pathname==='/api/templates')data={templates:[]};
   else if(u.pathname==='/api/source-exams')data={exams:[]};
   else if(u.pathname.startsWith('/api/assessments/'))data={assessment_id:key,answer_version:1,source_exam_id:null,title:'Synthetic',answer_key:[{question_no:1,correct_option:'A',points:2}]};
   else if(u.pathname==='/api/jobs')data={jobs:[summary(job),summary(other)]};
   else if(u.pathname.startsWith('/api/jobs/')){if(u.pathname.endsWith('f'.repeat(32)))return route.fulfill({status:409,json:{error:'任务不属于当前答案键'}});data={job:summary(u.pathname.split('/').at(-1)),submissions:[{submission_id:'current'}]};}
   else if(u.pathname==='/api/submissions')data={submissions:[{submission_id:'current',job_id:job,anonymous_code:'SYNTHETIC-CURRENT',state:'completed',score:0,max_score:2,page_count:1,mode:'omr'},{submission_id:'old',job_id:other,anonymous_code:'SYNTHETIC-OLD',state:'completed',score:2,max_score:2,page_count:1,mode:'omr'}]};
   else if(u.pathname==='/api/review-queue')data={items:[],count:0};else throw new Error('unexpected route '+u.pathname);
   return route.fulfill({json:data});
  });
  await page.goto('http://objective.synthetic/?assessment_id='+key+'&job_id='+job);
  await page.waitForFunction(()=>document.querySelector('#task-current').textContent.includes('处理中'));
  assert.equal(await page.locator('#results .result-card').count(),2);assert((await page.locator('#results').innerText()).includes('CURRENT'));assert((await page.locator('#results').innerText()).includes('OLD'));
  completed=true;await page.waitForFunction(()=>document.querySelector('#task-current').textContent.includes('已完成'));await page.waitForFunction(()=>!document.querySelector('#upload').disabled||!document.querySelector('#files').disabled);
  await page.reload();await page.waitForFunction(()=>document.querySelector('#task-current').textContent.includes('已完成'));
  assert(new URL(page.url()).searchParams.get('job_id')===job);assert.equal(await page.locator('#results .result-card').count(),2);
  await page.getByText('高级管理：本次任务与历史任务',{exact:true}).click();
  await page.locator('#result-scope').selectOption('job');await page.waitForFunction(()=>document.querySelectorAll('#results .result-card').length===1);
  await page.locator('#result-scope').selectOption('assessment');await page.waitForFunction(()=>document.querySelectorAll('#results .result-card').length===2);
  await page.getByText('查看最近任务（最多100组）',{exact:true}).click();assert.equal(await page.locator('.open-task').count(),2);
  await page.locator('.open-task').nth(1).click();await page.waitForURL('**job_id='+other);await page.waitForFunction(()=>document.querySelector('#task-current').textContent.includes('已完成'));
  assert(new URL(page.url()).searchParams.get('job_id')===other);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.goto('http://objective.synthetic/?assessment_id='+key+'&job_id='+'f'.repeat(32));await page.locator('#task-error').filter({hasText:'任务不属于当前答案键'}).waitFor();assert(await page.locator('#upload').isDisabled());
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);await page.close();console.log('PASS '+width+'px: reload restores exact job, history opens saved task, scoped results preserve zero, invalid job blocks, POST/model calls 0');
 }}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
