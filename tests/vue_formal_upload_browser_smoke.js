const {chromium}=require('playwright');const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{
 const [base,file,out,width]=process.argv.slice(2);const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});const page=await browser.newPage({viewport:{width:Number(width),height:1000}});
 const errors=[],requests=[];let previewBody,preview;
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST'){const path=new URL(r.url()).pathname;requests.push(path);if(path==='/api/ai-payload-preview')previewBody=r.postDataJSON()}});
 page.on('response',async r=>{if(new URL(r.url()).pathname==='/api/ai-payload-preview'&&r.status()===200)preview=await r.json()});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/upload');await page.getByLabel('用户名',{exact:true}).fill('test_teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByLabel('选择考试',{exact:true}).selectOption('a'.repeat(12));
  await page.getByText('更多设置',{exact:false}).click();await page.getByLabel('阅卷输入方式',{exact:true}).selectOption('auto_crop');
  if(Number(width)===1280)await page.getByLabel('关联班级（可选）',{exact:true}).selectOption('合成上传班');
  await page.waitForFunction(()=>document.querySelector('select')&&[...document.querySelectorAll('select')].some(s=>s.value==='e'.repeat(32)));
  await page.getByLabel('学生照片',{exact:false}).setInputFiles(file);
  const groupCheck=page.getByRole('checkbox',{name:'我已核对照片顺序、每份分组和考试版式'});await groupCheck.check();
  // Missing layout remains fail-closed before crop upload and before batch reservation.
  await page.route('**/api/exam-layout-profile?*',r=>r.fulfill({json:{exam_id:'e'.repeat(32),assignment_id:'a'.repeat(12),answer_regions_required:true,layout_profile:null}}));
  await page.getByRole('button',{name:'生成正式裁切预览（不调用模型）',exact:true}).click();await page.getByRole('alert').filter({hasText:'缺少有效答题区'}).waitFor();assert.equal(requests.includes('/api/student-auto-crop'),false);assert.equal(requests.includes('/api/batches'),false);
  await page.unroute('**/api/exam-layout-profile?*');
  await page.getByRole('button',{name:'生成正式裁切预览（不调用模型）',exact:true}).click();
  await page.getByRole('heading',{name:'2. 逐张验收正式裁切',exact:true}).waitFor();
  const cropChecks=page.getByRole('checkbox',{name:'题号正确、手写基本完整、未混入材料正文'});assert.equal(await cropChecks.count(),2);
  assert.equal(requests.includes('/api/student-auto-crop-review'),false);assert.equal(requests.includes('/api/submissions'),false);
  // Cancel a preview without generating approval credentials or a batch.
  await page.getByRole('button',{name:'取消并重新选择',exact:true}).click();assert.equal(requests.includes('/api/batches'),false);
  await groupCheck.check();await page.getByRole('button',{name:'生成正式裁切预览（不调用模型）',exact:true}).click();await cropChecks.first().waitFor();
  const accept=page.getByRole('button',{name:'裁切验收通过，继续隐私预览',exact:true});assert.equal(await accept.isDisabled(),true);await page.getByRole('button',{name:'已查看本批预览，全部验收',exact:true}).click();assert.equal(await accept.isEnabled(),true);await page.getByRole('button',{name:'取消全部验收',exact:true}).click();assert.equal(await accept.isDisabled(),true);await cropChecks.nth(0).check();assert.equal(await accept.isDisabled(),true);await cropChecks.nth(1).check();
  await page.screenshot({path:out+'-crops.png',fullPage:true});await accept.click();await page.getByRole('heading',{name:'3. 确认实际外发图片与隐私',exact:true}).waitFor();
  assert.equal(await page.locator('img[alt^="第1份外发图"]').count(),2);assert.equal(requests.includes('/api/batches'),false);
  const send=page.getByRole('button',{name:'确认并上传本批',exact:true});assert.equal(await send.isDisabled(),true);
  const denied=await page.evaluate(async({body,p})=>{
    const s=await(await fetch('/api/auth/session')).json();const headers={'Content-Type':'application/json','X-CSRF-Token':s.csrf_token};
    const upload={...body,answer_regions:p.answer_regions,ai_privacy_confirmed:true,privacy_status:'unchecked',ai_privacy_preview_token:p.preview_token,ai_privacy_fingerprint:p.preview_fingerprint};
    const unchecked=await fetch('/api/submissions',{method:'POST',headers,body:JSON.stringify(upload)});
    const fake=structuredClone(upload);fake.privacy_status='no_identity_visible';delete fake.photos[0].answer_region_attestation;
    const forged=await fetch('/api/submissions',{method:'POST',headers,body:JSON.stringify(fake)});
    return [unchecked.status,forged.status];
  },{body:previewBody,p:preview});assert.deepEqual(denied,[400,400]);
  const beforeUploads=requests.filter(p=>p==='/api/submissions').length;
  await page.getByLabel('第1份隐私状态',{exact:true}).selectOption('no_identity_visible');assert.equal(await send.isDisabled(),true);
  await page.getByRole('checkbox',{name:'我已逐张查看实际外发图片，确认没有未排除的身份信息，并同意提交双模型批改'}).check();
  await page.screenshot({path:out+'-privacy.png',fullPage:true});await send.click();await page.getByRole('heading',{name:'上传完成',exact:true}).waitFor();
  assert.equal(requests.filter(p=>p==='/api/batches').length,1);assert.equal(requests.filter(p=>p==='/api/submissions').length,beforeUploads+1);
  assert.ok(requests.indexOf('/api/student-auto-crop-review')<requests.indexOf('/api/ai-payload-preview'));assert.ok(requests.indexOf('/api/ai-payload-preview')<requests.indexOf('/api/batches'));
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
  await page.screenshot({path:out+'-complete.png',fullPage:true});
  // Losing the batch-reservation response must stop, not silently re-reserve/re-upload.
  await page.getByRole('link',{name:'终评与成绩',exact:true}).click();await page.getByRole('link',{name:'上传与批改',exact:true}).click();
  await page.getByLabel('选择考试',{exact:true}).selectOption('a'.repeat(12));if(Number(width)===1280)await page.getByLabel('关联班级（可选）',{exact:true}).selectOption('合成上传班');
  await page.getByText('更多设置',{exact:false}).click();await page.getByLabel('阅卷输入方式',{exact:true}).selectOption('auto_crop');await page.waitForFunction(()=>document.querySelector('#upload-exam').value==='e'.repeat(32));await page.getByLabel('学生照片',{exact:false}).setInputFiles(file);await groupCheck.check();
  await page.getByRole('button',{name:'生成正式裁切预览（不调用模型）',exact:true}).click();await cropChecks.first().waitFor();await cropChecks.nth(0).check();await cropChecks.nth(1).check();await accept.click();
  await page.getByLabel('第1份隐私状态',{exact:true}).selectOption('no_identity_visible');await page.getByRole('checkbox',{name:'我已逐张查看实际外发图片，确认没有未排除的身份信息，并同意提交双模型批改'}).check();
  const uploadsBeforeAbort=requests.filter(p=>p==='/api/submissions').length;await page.route('**/api/batches',r=>r.request().method()==='POST'?r.abort():r.continue());await send.click();
  await page.getByRole('heading',{name:'请核实批次状态',exact:true}).waitFor();assert.equal(requests.filter(p=>p==='/api/submissions').length,uploadsBeforeAbort);assert.equal(await send.count(),0);
  assert.equal(requests.filter(p=>p==='/api/batches').length,2);assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'.json',JSON.stringify({passed:true,width:Number(width),requests,denied,uncertain_write_stops:true,errors},null,2));
 }catch(e){fs.writeFileSync(out+'-failure.txt',JSON.stringify({text:await page.locator('body').innerText(),requests,errors},null,2));await page.screenshot({path:out+'-failure.png',fullPage:true});throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
