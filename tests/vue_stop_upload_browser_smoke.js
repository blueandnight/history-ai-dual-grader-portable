const {chromium}=require('playwright');const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{
 const [base,file1,file2,out,width]=process.argv.slice(2);const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});const page=await browser.newPage({viewport:{width:Number(width),height:1000}});
 const errors=[],requests=[],batchBodies=[],submissionBodies=[];let finalPreviewBody,finalPreview;
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST'){const path=new URL(r.url()).pathname;requests.push(path);if(path==='/api/ai-payload-preview'){const body=r.postDataJSON();if(body.preview_stage!=='prepare')finalPreviewBody=body}if(path==='/api/batches')batchBodies.push(r.postDataJSON());if(path==='/api/submissions')submissionBodies.push(r.postDataJSON())}});
 page.on('response',async r=>{if(new URL(r.url()).pathname==='/api/ai-payload-preview'&&r.status()===200){const body=await r.json();if(body.preview_token)finalPreview=body}});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/upload');await page.getByLabel('用户名',{exact:true}).fill('test_teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByLabel('选择阅卷作业',{exact:true}).selectOption('a'.repeat(12));if(Number(width)===1280)await page.getByLabel('关联班级',{exact:true}).selectOption('合成上传班');
  await page.waitForFunction(()=>document.querySelector('#upload-exam').value==='e'.repeat(32));
  const sources=[{name:'01.png',mimeType:'image/png',buffer:fs.readFileSync(file1)},{name:'02.png',mimeType:'image/png',buffer:fs.readFileSync(file2)}];
  await page.getByLabel('学生照片',{exact:false}).setInputFiles(sources);if(Number(width)===390)await page.getByLabel('每份照片张数').fill('1');
  const groupCheck=page.getByRole('checkbox',{name:'我已核对照片顺序、每份分组和考试版式'});await groupCheck.check();
  await page.route('**/api/exam-layout-profile?*',r=>r.fulfill({json:{exam_id:'e'.repeat(32),assignment_id:'a'.repeat(12),answer_regions_required:false,layout_profile_id:'',profile_invalid:true,layout_profile:null,subjective_question_ids:[]}}));
  const prepare=page.getByRole('button',{name:'核对分组并编辑原照片隐私',exact:true});await prepare.click();await page.getByRole('heading',{name:'2. 编辑原照片并确认实际外发图片',exact:true}).waitFor();
  assert.equal(await page.locator('img[alt^="第1份外发图"]').count(),1);assert.equal(requests.includes('/api/student-auto-crop'),false);assert.equal(requests.includes('/api/batches'),false);
  await page.getByRole('button',{name:'取消本批',exact:true}).click();assert.equal(requests.includes('/api/batches'),false);
  await groupCheck.check();await prepare.click();await page.getByRole('heading',{name:'2. 编辑原照片并确认实际外发图片',exact:true}).waitFor();
  await page.getByRole('button',{name:'单独遮挡修正本份（旧预览失效）',exact:true}).first().click();const canvas=page.getByTestId('privacy-canvas');await canvas.locator('img[alt="本机待编辑学生页"]').waitFor();
  const box=await canvas.boundingBox();await page.mouse.move(box.x+box.width*.08,box.y+box.height*.08);await page.mouse.down();await page.mouse.move(box.x+box.width*.2,box.y+box.height*.15);await page.mouse.up();
  await page.getByRole('button',{name:'生成实际外发预览',exact:true}).click();await page.getByRole('heading',{name:'实际将发给模型的图片',exact:true}).waitFor();
  await page.getByRole('checkbox',{name:'已逐张核对外发图，身份信息已处理，答案未被误遮盖'}).check();await page.getByRole('button',{name:'采用这组外发预览',exact:true}).click();
  assert.equal(finalPreviewBody.grading_input_mode,'original_photo');assert.equal(finalPreviewBody.exam_id,'e'.repeat(32));assert.deepEqual(finalPreviewBody.answer_regions,[]);assert.ok(finalPreviewBody.photos[0].ai_redactions.length>0);assert.ok(finalPreview.images.every(i=>i.image_kind==='full_page'));
  assert.equal(await page.getByRole('button',{name:'应用到尚未确认的答卷'}).count(),1);await page.getByLabel('本批隐私状态',{exact:true}).selectOption('redacted');await page.getByRole('button',{name:'应用到尚未确认的答卷',exact:true}).click();
  const send=page.getByRole('button',{name:'确认并上传本批',exact:true});assert.equal(await send.isDisabled(),true);await page.getByRole('checkbox',{name:'我已逐张查看实际外发图片，确认没有未排除的身份信息，并同意提交双模型批改'}).check();await page.screenshot({path:out+'-privacy.png',fullPage:true});let releaseFirst;
  await page.route('**/api/submissions',async route=>{if(route.request().method()==='POST'&&route.request().postDataJSON().sequence===1){await new Promise(resolve=>releaseFirst=resolve)}await route.continue()});
  await send.click();await page.getByRole('button',{name:'停止后续上传',exact:true}).waitFor();
  await page.getByRole('button',{name:'停止后续上传',exact:true}).click();releaseFirst();
  await page.getByRole('heading',{name:'上传已停止',exact:true}).waitFor();assert.equal(submissionBodies.length,1);
  assert.equal(requests.filter(p=>p==='/api/batches/skip').length,0);
  await page.getByRole('button',{name:'原位上传下一份',exact:true}).click();
  await page.getByRole('heading',{name:'上传完成',exact:true}).waitFor();
  assert.equal(batchBodies.length,1);assert.deepEqual(submissionBodies.map(b=>b.sequence),[1,2]);
  assert.equal(requests.filter(p=>p==='/api/batches').length,1);assert.equal(requests.filter(p=>p==='/api/submissions').length,2);assert.equal(batchBodies[0].grading_input_mode,'original_photo');assert.ok(submissionBodies.every(body=>body.grading_input_mode==='original_photo'&&body.exam_id==='e'.repeat(32)));assert.equal(requests.filter(p=>p==='/api/student-auto-crop').length,0);assert.equal(requests.filter(p=>p==='/api/student-auto-crop-review').length,0);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'.json',JSON.stringify({passed:true,width:Number(width),requests,grading_input_mode:'original_photo',full_page_preview:true,crop_requests:0,errors},null,2));
 }catch(e){fs.writeFileSync(out+'-failure.txt',JSON.stringify({text:await page.locator('body').innerText(),requests,errors},null,2));await page.screenshot({path:out+'-failure.png',fullPage:true});throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
