const {chromium}=require('playwright');
const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{
 const [base,out,width,word,pdf]=process.argv.slice(2);const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
 const context=await browser.newContext({viewport:{width:Number(width),height:900}});const page=await context.newPage();const errors=[],creates=[];
 page.on('pageerror',e=>errors.push(String(e)));
 page.on('request',r=>{if(new URL(r.url()).pathname==='/api/create-imported-exam')creates.push(r.postDataJSON())});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/create');await page.getByLabel('用户名',{exact:true}).fill('test_teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByRole('heading',{name:'新建考试',exact:true}).waitFor();
  await page.getByLabel('PDF试题',{exact:false}).setInputFiles(pdf);await page.getByRole('button',{name:'解析为草稿',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'文件解析未完成'}).waitFor();assert.equal(creates.length,0);
  await page.getByLabel('PDF试题',{exact:false}).setInputFiles([]);await page.getByLabel('Word试题',{exact:false}).setInputFiles(word);
  const parsed=page.waitForResponse(r=>r.url().endsWith('/api/import-exam-draft')&&r.status()===200);
  await page.getByRole('button',{name:'解析为草稿',exact:true}).click();const draft=await(await parsed).json();
  await page.getByLabel('考试名称',{exact:true}).waitFor();assert.equal(creates.length,0);
  const guard=await page.evaluate(async({id})=>{
    const s=await(await fetch('/api/auth/session')).json();
    const noCsrf=await fetch('/api/create-imported-exam',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({draft_id:id,confirmation:{}})});
    const unchecked=await fetch('/api/create-imported-exam',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':s.csrf_token},body:JSON.stringify({draft_id:id,confirmation:{teacher_confirmed:false}})});
    return [noCsrf.status,unchecked.status];
  },{id:draft.draft_id});assert.deepEqual(guard,[403,400]);creates.length=0;
  await page.getByLabel('考试名称',{exact:true}).fill('合成草稿');await page.getByLabel('9.1评分方式',{exact:true}).selectOption('weighted');await page.getByLabel('9.1评分点1分值',{exact:true}).fill('1');await page.getByLabel('9.1评分点2分值',{exact:true}).fill('3');
  if(await page.getByLabel('客观题批量分值',{exact:true}).count()){await page.getByLabel('客观题批量分值',{exact:true}).fill('3');await page.getByRole('button',{name:'应用分值到全部客观题',exact:true}).click()}
  const check=page.getByRole('checkbox',{name:'我已核对题目、答案、参考答案、满分和所有评分点'});const submit=page.getByRole('button',{name:'确认创建考试',exact:true});
  assert.equal(await submit.isDisabled(),true);await check.check();assert.equal(await submit.isEnabled(),true,await page.locator('form').innerText());
  if(Number(width)===390){
   await page.getByLabel('9.1评分方式',{exact:true}).selectOption('point_bank');assert.equal(await check.isChecked(),false);
   await page.getByLabel('9.1每点分值',{exact:true}).fill('2');await page.getByLabel('9.1满分',{exact:true}).fill('8');
   for(let i=0;i<3;i++)await page.getByRole('button',{name:'添加评分点',exact:true}).click();
   for(let i=1;i<=5;i++){await page.getByLabel('9.1评分点'+i+'文字',{exact:true}).fill('合成独立要点'+i);await page.getByLabel('9.1评分点'+i+'分值',{exact:true}).fill('2')}
   await check.check();assert.equal(await submit.isEnabled(),true);
   await page.getByLabel('9.1评分点1分值',{exact:true}).fill('1');await check.check();assert.equal(await submit.isDisabled(),true);
   await page.getByLabel('9.1评分点1分值',{exact:true}).fill('2');await check.check();assert.equal(await submit.isEnabled(),true);
  }
  await page.getByLabel('考试名称',{exact:true}).fill('合成创建'+width);assert.equal(await check.isChecked(),false);await check.check();
  await page.route('**/api/create-imported-exam',async r=>{await new Promise(resolve=>setTimeout(resolve,250));await r.continue()});
  await submit.click();assert.equal(await page.getByRole('button',{name:'正在创建…',exact:true}).isDisabled(),true);await page.locator('form').evaluate(f=>f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));await page.getByRole('heading',{name:'考试已创建',exact:true}).waitFor();
  assert.equal(creates.length,1);assert.equal(creates[0].confirmation.teacher_confirmed,true);
  const saved=creates[0].confirmation.material_questions[0].sub_questions[0];
  assert.deepEqual(saved.rubric_draft.map(p=>Number(p.score)),Number(width)===390?[2,2,2,2,2]:[1,3]);
  assert.equal(saved.scoring_mode,Number(width)===390?'point_bank':'weighted');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
  await page.screenshot({path:out+'/exam-created-'+width+'.png',fullPage:true});
  // An uncertain write must not offer a second click or an automatic retry.
  await page.getByRole('link',{name:'历史记录',exact:true}).click();await page.getByRole('link',{name:'新建考试',exact:true}).click();
  await page.getByLabel('Word试题',{exact:false}).setInputFiles(word);await page.getByRole('button',{name:'解析为草稿',exact:true}).click();
  await page.getByLabel('考试名称',{exact:true}).fill('合成不确定创建');
  await page.getByLabel('9.1评分方式',{exact:true}).selectOption('weighted');
  await page.getByLabel('9.1评分点1分值',{exact:true}).fill('1');await page.getByLabel('9.1评分点2分值',{exact:true}).fill('3');
  await check.check();await page.unroute('**/api/create-imported-exam');
  await page.route('**/api/create-imported-exam',r=>r.abort());await page.getByRole('button',{name:'确认创建考试',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'创建结果未能确认'}).waitFor();assert.equal(await page.getByRole('button',{name:'确认创建考试',exact:true}).isDisabled(),true);
  assert.equal(creates.length,2);assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'/browser-'+width+'.json',JSON.stringify({csrf:guard,create_requests:creates.length,errors},null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
