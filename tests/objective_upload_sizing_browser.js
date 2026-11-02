// Synthetic browser responses only. No server/database/model calls.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const script=fs.readFileSync(path.join(root,'objective_static/app.js'),'utf8');
const context={URLSearchParams,location:{search:''},document:{documentElement:{dataset:{}},addEventListener(){}},window:{addEventListener(){}}};
vm.createContext(context);vm.runInContext(script,context);
const item=(i,width=3072,height=4096)=>({file:i,size:4*1024*1024,width,height});
const plan=context.planUploadBatches;
const batches=plan(Array.from({length:51},(_,i)=>item(i)),'one_per_image');
assert.deepEqual(Array.from(batches.flat()),Array.from({length:51},(_,i)=>i));
assert.ok(batches.length>1);
for(const batch of batches)assert.ok(batch.length*(3072*4096*4+1024*1024)<=180*1024*1024);
assert.throws(()=>plan(Array.from({length:51},(_,i)=>item(i)),'multi_page_all'),/不会拆成不同考生/);
assert.equal(plan([item(1),item(2)],'multi_page_all').length,1);
assert.throws(()=>plan([{...item(0),size:13*1024*1024}],'one_per_image'),/12 MiB/);
assert.throws(()=>plan([item(0,9000,9000)],'one_per_image'),/尺寸/);

(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
 try{for(const width of [1280,390]){
  const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  let received=[],failAt=0,expectedPhoto,hold=false;
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url());
   if(url.origin!=='http://objective.synthetic')return route.abort();
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(root,'objective_static/index.html'),'utf8').replaceAll('__OBJECTIVE_BASE__','')});
   if(url.pathname==='/static/app.js')return route.fulfill({contentType:'text/javascript',body:script});
   if(url.pathname==='/static/styles.css')return route.fulfill({contentType:'text/css',body:fs.readFileSync(path.join(root,'objective_static/styles.css'),'utf8')});
   let data;
   if(url.pathname==='/api/auth/session')data={authenticated:true,csrf_token:'synthetic-only'};
   else if(url.pathname==='/api/status')data={recognizer:'synthetic',vision_grading:{enabled:true,available:true,models:[]}};
   else if(url.pathname==='/api/templates')data={templates:[]};
   else if(url.pathname==='/api/source-exams')data={exams:[]};
   else if(url.pathname.startsWith('/api/assessments/'))data={assessment_id:'a'.repeat(32),answer_version:1,source_exam_id:null,title:'Synthetic only',answer_key:Array.from({length:20},(_,i)=>({question_no:i+1,correct_option:'A',points:1}))};
   else if(url.pathname==='/api/jobs')data={jobs:[]};
   else if(url.pathname==='/api/submissions')data={submissions:[]};
   else if(url.pathname==='/api/review-queue')data={items:[],count:0};
   else if(url.pathname==='/api/uploads'){
    const body=req.postDataBuffer();
    const names=[...body.toString('latin1').matchAll(/name="files"; filename="([^"]+)"/g)].map(m=>m[1]);
    received.push(names);
    let offset=0;for(let i=0;i<names.length;i++){const at=body.indexOf(expectedPhoto,offset);assert.ok(at>=0,'original image bytes must be preserved');offset=at+expectedPhoto.length;}
    assert.ok(body.includes(Buffer.from('no_identity_visible')));
    assert.ok(body.includes(Buffer.from('one_per_image')));
    if(failAt&&received.length===failAt)return route.abort('failed');
    data={job_id:String(received.length)};
   }else if(url.pathname.startsWith('/api/jobs/')){const n=received.at(-1).length;data={job:{status:hold?'processing':'completed',total_submissions:n,processed_submissions:n,complete_submissions:n,review_submissions:0,error_submissions:0,total_files:n,grouping:'one_per_image'}}}
   else throw new Error('Unexpected synthetic request '+url.pathname);
   await route.fulfill({json:data});
  });
  await page.goto('http://objective.synthetic/?assessment_id='+'a'.repeat(32));
  await page.waitForFunction(()=>document.querySelector('#assessment-state').textContent.includes('历史答案键'));
  const dataUrl=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=3072;c.height=4096;const x=c.getContext('2d');x.fillStyle='white';x.fillRect(0,0,c.width,c.height);x.fillStyle='black';x.font='60px sans-serif';x.fillText('SYNTHETIC ONLY A B C D',100,300);return c.toDataURL('image/png')});
  const buffer=Buffer.from(dataUrl.split(',')[1],'base64');expectedPhoto=buffer;
  const files=Array.from({length:51},(_,i)=>({name:`synthetic-${i+1}.png`,mimeType:'image/png',buffer}));
  await page.locator('#files').setInputFiles(files);
  assert.equal(await page.locator('#upload').isDisabled(),true); // privacy stays mandatory
  await page.locator('#privacy-status').selectOption('no_identity_visible');
  await page.locator('#upload').click();
  await page.waitForFunction(()=>document.querySelector('#progress').textContent.includes('整批处理结束'),{},{timeout:60000});
  assert.equal(received.length,batches.length);
  assert.deepEqual(received.flat(),files.map(f=>f.name));
  assert.equal(await page.locator('#files').evaluate(e=>e.files.length),0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  // Never split the pages of one candidate into multiple submissions.
  received=[];await page.locator('input[value="multi_page_all"]').check();
  await page.locator('#files').setInputFiles(files);await page.locator('#upload').click();
  await page.getByText(/系统不会拆成不同考生/).waitFor();assert.equal(received.length,0);
  // Explicit stop never aborts an accepted model request or posts another group.
  received=[];hold=true;await page.locator('input[value="one_per_image"]').check();await page.locator('#files').setInputFiles(files);await page.locator('#upload').click();
  await page.waitForFunction(()=>document.querySelector('#task-current').textContent.includes('处理中'));
  await page.locator('#stop-uploads').click();hold=false;
  await page.getByText(/已停止后续上传；已接收/).waitFor();assert.equal(received.length,1);
  assert.equal(await page.locator('#files').evaluate(e=>e.files.length),51-received[0].length);
  received=[];await page.locator('#files').setInputFiles(files);
  // A lost response stops the chain; already accepted and uncertain chunks are not resent.
  await page.locator('input[value="one_per_image"]').check();
  failAt=2;await page.locator('#upload').click();
  await page.getByText(/本组接收结果未能确认/).waitFor();
  assert.equal(received.length,2);
  assert.equal(await page.locator('#files').evaluate(e=>e.files.length),51-received.flat().length);
  assert.deepEqual(errors,[]);
  await page.close();
 }}finally{await browser.close()}
 console.log('PASS: 51 original photos desktop/mobile; bounded chunks, no duplicates; privacy, multi-page and lost-response guards; real API/model calls 0');
})().catch(e=>{console.error(e);process.exitCode=1});
