const {chromium}=require('playwright');const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{
 const [base,word,photo,out,width,count]=process.argv.slice(2);const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});const page=await browser.newPage({viewport:{width:Number(width),height:1000}});const errors=[],writes=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')writes.push({path:new URL(r.url()).pathname,body:r.postData()})});await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/create');await page.getByLabel('用户名',{exact:true}).fill('test_teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByLabel('Word试题',{exact:false}).setInputFiles(word);await page.getByRole('button',{name:'解析为草稿',exact:true}).click();await page.getByLabel('考试名称',{exact:true}).waitFor();
  const check=page.getByRole('checkbox',{name:'我已核对题目、答案、参考答案、满分和所有评分点'}),create=page.getByRole('button',{name:'确认创建考试',exact:true});
  await check.check();assert.equal(await create.isDisabled(),true);assert.equal(await page.getByLabel('第1题分值',{exact:true}).inputValue(),'');
  await page.getByLabel('客观题批量分值',{exact:true}).fill('3');await page.getByRole('button',{name:'应用分值到全部客观题',exact:true}).click();assert.equal(await check.isChecked(),false);
  await page.getByLabel('第20题分值',{exact:true}).fill('4');
  for(const q of ['21.1','22.1']){await page.getByLabel(q+'满分',{exact:true}).fill('4');for(let i=1;i<=2;i++)await page.getByLabel(q+'评分点'+i+'分值',{exact:true}).fill('2')}
  await page.getByLabel('考试名称',{exact:true}).fill('合成统一考试'+width);await check.check();assert.equal(await create.isEnabled(),true,await page.locator('form').innerText());
  const response=page.waitForResponse(r=>r.url().endsWith('/api/create-imported-exam')&&r.status()===201);await create.click();const saved=await(await response).json();assert.deepEqual(saved.assignment.score_totals,{objective:61,subjective:8,paper:69});assert.equal(saved.assignment.max_score,8);assert.ok((await page.getByRole('link',{name:'上传本考试主观题答卷'}).getAttribute('href')).includes('assignment='+saved.assignment.id+'&exam='+saved.exam_id));
  await page.getByRole('link',{name:'使用本考试批改客观题',exact:true}).click();const objective=page.frameLocator('iframe[title="客观题上传与复核"]');await objective.locator('#source-exam').waitFor({state:'attached'});const frame=page.frames().find(f=>f.url().includes('embedded=1'));await objective.locator('#source-exam option[value="'+saved.exam_id+'"]').waitFor({state:'attached'});await frame.waitForFunction(exam=>document.querySelector('#source-exam').value===exam,saved.exam_id);assert.equal(await objective.locator('#source-exam').isDisabled(),true);
  const use=objective.getByRole('button',{name:'使用已确认考试生成或复用只读快照',exact:true});await frame.waitForFunction(()=>!document.querySelector('#use-source-exam').disabled);
  assert.equal(await objective.locator('#source-preview article').count(),20);assert.equal(await objective.locator('#source-preview input,#source-preview select').count(),0);assert.equal(await objective.locator('#independent-key').isVisible(),false);
  assert.equal(writes.filter(w=>w.path==='/objective/api/assessments/from-exam').length,0);
  let ready=page.waitForResponse(r=>r.url().endsWith('/api/assessments/from-exam'));await use.click();const first=await(await ready).json();assert.equal(first.source_exam_id,saved.exam_id);assert.equal(first.max_score,61);
  ready=page.waitForResponse(r=>r.url().endsWith('/api/assessments/from-exam'));await use.click();const second=await(await ready).json();assert.equal(second.assessment_id,first.assessment_id);assert.equal(second.reused,true);
  await objective.locator('#mode').selectOption('vision');await objective.locator('#files').setInputFiles(Number(count||1)===5?[1,2,3,4,5].map(i=>photo.replace('omr-1.png',`omr-${i}.png`)):photo);assert.equal(await objective.locator('#upload').isDisabled(),true); // privacy remains mandatory
  await objective.locator('#mode').selectOption('omr');await objective.locator('#upload').click();await frame.waitForFunction(()=>document.querySelector('#results').textContent.includes('61 / 61'),{},{timeout:60000});
  assert.equal(writes.filter(w=>w.path==='/objective/api/assessments').length,0);
  await page.waitForFunction(id=>location.hash.includes('assessment='+id),first.assessment_id);
  const snapshotWrites=writes.filter(w=>w.path==='/objective/api/assessments/from-exam').length;
  await page.reload();await objective.locator('#results .result-card').first().waitFor();
  assert.equal(writes.filter(w=>w.path==='/objective/api/assessments/from-exam').length,snapshotWrites);
  assert.equal(await objective.locator('#independent-key').isVisible(),false);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  assert.deepEqual(errors,[]);await page.screenshot({path:out+'.png',fullPage:true});fs.writeFileSync(out+'.json',JSON.stringify({saved,snapshot:first,passed:true,errors},null,2));
 }catch(e){fs.writeFileSync(out+'-failure.txt',await page.locator('body').innerText());throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
