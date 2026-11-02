const {chromium}=require('playwright');const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{
 const [base,file1,file2,out,width]=process.argv.slice(2);const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});const page=await browser.newPage({viewport:{width:Number(width),height:1000}});
 const errors=[],requests=[],batchBodies=[],submissionBodies=[];let finalPreviewBody,finalPreview;
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST'){const path=new URL(r.url()).pathname;requests.push(path);if(path==='/api/ai-payload-preview'){const body=r.postDataJSON();if(body.preview_stage!=='prepare')finalPreviewBody=body}if(path==='/api/batches')batchBodies.push(r.postDataJSON());if(path==='/api/submissions')submissionBodies.push(r.postDataJSON())}});
 page.on('response',async r=>{if(new URL(r.url()).pathname==='/api/ai-payload-preview'&&r.status()===200){const body=await r.json();if(body.preview_token)finalPreview=body}});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/upload');await page.getByLabel('用户名',{exact:true}).fill('test_teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByLabel('当前考试',{exact:true}).selectOption('a'.repeat(12));if(Number(width)===1280)await page.getByLabel('关联班级（可选）',{exact:true}).selectOption('合成上传班');
  await page.waitForFunction(()=>document.querySelector('#upload-exam').value==='e'.repeat(32));
  assert.equal(await page.locator('#upload-layout').count(),0);
  await page.getByText('更多设置',{exact:false}).click();await page.getByLabel('阅卷输入方式',{exact:true}).selectOption('auto_crop');
  await page.locator('#upload-layout').waitFor();
  if(Number(width)===390){
   assert.equal(await page.locator('#upload-exam').inputValue(),'');
   assert.equal(await page.locator('#upload-exam option').count(),1);
   assert.equal(await page.getByRole('button',{name:'生成正式裁切预览（不调用模型）',exact:true}).isDisabled(),true);
  }else await page.waitForFunction(()=>document.querySelector('#upload-exam').value==='e'.repeat(32));
  await page.getByLabel('阅卷输入方式',{exact:true}).selectOption('original_photo');
  await page.waitForFunction(()=>document.querySelector('#upload-exam').value==='e'.repeat(32));
  assert.equal(await page.locator('#upload-layout').count(),0);
  const sources=[{name:'01.png',mimeType:'image/png',buffer:fs.readFileSync(file1)},{name:'02.png',mimeType:'image/png',buffer:fs.readFileSync(file2)}];
  await page.getByLabel('学生照片',{exact:false}).setInputFiles(sources);if(Number(width)===390)await page.getByLabel('每份照片张数').fill('1');
  const groupCheck=page.getByRole('checkbox',{name:'我已核对照片顺序、每份分组和考试版式'});await groupCheck.check();
  await page.route('**/api/exam-layout-profile?*',r=>r.fulfill({json:{exam_id:'e'.repeat(32),assignment_id:'a'.repeat(12),answer_regions_required:false,layout_profile_id:'',profile_invalid:true,layout_profile:null,subjective_question_ids:[]}}));
  const prepare=page.getByRole('button',{name:'核对分组并编辑原照片隐私',exact:true});await prepare.click();await page.getByRole('heading',{name:'2. 编辑原照片并确认实际外发图片',exact:true}).waitFor();
  assert.equal(await page.locator('img[alt^="第1份外发图"]').count(),1);assert.equal(requests.includes('/api/student-auto-crop'),false);assert.equal(requests.includes('/api/batches'),false);
  await page.getByRole('button',{name:'取消本批',exact:true}).click();assert.equal(requests.includes('/api/batches'),false);
  await groupCheck.check();await prepare.click();await page.getByRole('heading',{name:'2. 编辑原照片并确认实际外发图片',exact:true}).waitFor();
  await page.getByRole('button',{name:'单独遮挡修正本份（旧预览失效）',exact:true}).first().click();const canvas=page.getByTestId('privacy-canvas');await canvas.locator('img[alt="本机待编辑学生页"]').waitFor();
  await canvas.scrollIntoViewIfNeeded();await canvas.locator('img').evaluate(img=>img.complete?Promise.resolve():new Promise(resolve=>img.onload=resolve));const box=await canvas.boundingBox();await page.mouse.move(box.x+box.width*.08,box.y+box.height*.08);await page.mouse.down();await page.mouse.move(box.x+box.width*.2,box.y+box.height*.15);await page.mouse.up();
  await page.getByRole('button',{name:'生成实际外发预览',exact:true}).click();await page.getByRole('heading',{name:'实际将发给模型的图片',exact:true}).waitFor();
  await page.getByRole('checkbox',{name:'已逐张核对外发图，身份信息已处理，答案未被误遮盖'}).check();await page.getByRole('button',{name:'采用这组外发预览',exact:true}).click();
  assert.equal(finalPreviewBody.grading_input_mode,'original_photo');assert.equal(finalPreviewBody.exam_id,'e'.repeat(32));assert.deepEqual(finalPreviewBody.answer_regions,[]);assert.ok(finalPreviewBody.photos[0].ai_redactions.length>0);assert.ok(finalPreview.images.every(i=>i.image_kind==='full_page'));
  const consent=page.getByRole('checkbox',{name:'我已逐张查看实际外发图片，确认没有未排除的身份信息，并同意提交双模型批改'});
  await consent.check();assert.equal(await page.getByRole('button',{name:'确认并上传本批',exact:true}).isDisabled(),true);
  await page.getByRole('button',{name:'定位未设置的答卷',exact:true}).click();assert.equal(await page.locator('#upload-privacy-0').evaluate(el=>el===document.activeElement),true);
  await page.getByLabel('第1份隐私状态',{exact:true}).selectOption('redacted');await page.getByLabel('统一设置本批隐私状态',{exact:true}).selectOption('no_identity_visible');await page.getByRole('button',{name:'应用本批隐私状态',exact:true}).click();
  assert.equal(await page.getByLabel('第1份隐私状态',{exact:true}).inputValue(),'redacted');assert.equal(await page.getByLabel('第2份隐私状态',{exact:true}).inputValue(),'no_identity_visible');assert.equal(await consent.isChecked(),false);await page.getByText('尚有0份未设置',{exact:false}).waitFor();
  const send=page.getByRole('button',{name:'确认并上传本批',exact:true});assert.equal(await send.isDisabled(),true);await page.getByRole('checkbox',{name:'我已逐张查看实际外发图片，确认没有未排除的身份信息，并同意提交双模型批改'}).check();await page.screenshot({path:out+'-privacy.png',fullPage:true});await send.click();await page.getByRole('heading',{name:'上传完成',exact:true}).waitFor();
  assert.equal(requests.filter(p=>p==='/api/batches').length,1);assert.equal(requests.filter(p=>p==='/api/submissions').length,2);assert.equal(batchBodies[0].grading_input_mode,'original_photo');assert.ok(submissionBodies.every(body=>body.grading_input_mode==='original_photo'&&body.exam_id==='e'.repeat(32)));assert.equal(requests.filter(p=>p==='/api/student-auto-crop').length,0);assert.equal(requests.filter(p=>p==='/api/student-auto-crop-review').length,0);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);assert.deepEqual(errors,[]);
  // Real scoped API and batch adoption stay on upload; live refresh cannot change consent.
  const batchTable=page.getByRole('region',{name:'本批双评成绩',exact:true});
  await batchTable.getByRole('button',{name:'批量采纳所选 2 份',exact:true}).waitFor({timeout:30000});
  let passiveReads=0;
  const forcePending=async route=>{passiveReads++;const response=await route.fetch();const data=await response.json();data.papers[1].state='processing';data.papers[1].progress_state='grading';await route.fulfill({response,json:data})};
  await page.route('**/api/batch-adoption?*',forcePending);
  await batchTable.getByRole('button',{name:'刷新双评成绩',exact:true}).click();
  const adoptionConsent=batchTable.getByRole('checkbox',{name:/我已核对所选答卷及共识状态/});
  await batchTable.getByRole('button',{name:'批量采纳所选 1 份',exact:true}).waitFor();
  await adoptionConsent.check();const beforeReads=passiveReads;
  await page.waitForTimeout(3400);assert(await adoptionConsent.isChecked());assert.equal(passiveReads,beforeReads);
  await page.unroute('**/api/batch-adoption?*',forcePending);
  await batchTable.getByRole('button',{name:'刷新双评成绩',exact:true}).click();
  await batchTable.getByRole('button',{name:'批量采纳所选 2 份',exact:true}).waitFor();
  await batchTable.getByRole('checkbox',{name:/^采纳 /}).nth(1).uncheck();await adoptionConsent.check();
  await batchTable.getByRole('button',{name:'批量采纳所选 1 份',exact:true}).click();
  await batchTable.locator('.saved-score').filter({hasText:'共识采纳'}).waitFor();
  assert((await page.evaluate(()=>location.hash)).startsWith('#/upload'));
  // Upload -> automatic status reads -> existing teacher review, without visiting history.
  for(let paper=1;paper<=2;paper++){
   const scores=page.getByRole('region',{name:'本批双评成绩',exact:true});
   const row=scores.locator('.history-table>table>tbody>tr').filter({hasText:`第${paper}份 ·`});
   const enter=row.getByRole('button',{name:/教师.*终评$/,exact:true});await enter.waitFor({timeout:30000});
   await row.getByRole('group',{name:'小题双评对照'}).waitFor();
   assert((await row.getByRole('group',{name:'小题双评对照'}).innerText()).includes('17.1'));
   assert((await page.evaluate(()=>location.hash)).startsWith('#/upload'));
   await page.screenshot({path:out+`-scores-${paper}.png`,fullPage:true});
   assert.equal(requests.includes('/api/confirm'),paper>1);
   await enter.click();const root=page.getByRole('region',{name:'教师逐点终评',exact:true});
   const save=root.getByRole('button',{name:'保存教师终评',exact:true});await save.waitFor();assert.equal(await save.isDisabled(),true);
   await root.getByRole('button',{name:'查看原图第1张',exact:true}).click();
   await root.getByRole('checkbox',{name:'我已打开原图并核对模型证据',exact:true}).check();
   await root.getByRole('button',{name:'记录人工复核',exact:true}).click();await root.getByRole('status').filter({hasText:'已保存原图人工复核'}).waitFor();
   const reopen=root.getByRole('button',{name:'重新逐点改判（保存后留审计记录）',exact:true});if(await reopen.count())await reopen.click();
   const points=root.locator('select[id^="teacher-"]');assert.ok(await points.count()>0);
   for(let i=0;i<await points.count();i++)await points.nth(i).selectOption('teacher_none');
   await save.click();await root.getByRole('status').filter({hasText:'教师终评已保存'}).waitFor();
   assert.ok((await page.evaluate(()=>location.hash)).includes('exam='+'e'.repeat(32)));
   page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'返回本批成绩',exact:true}).click();
   await root.waitFor({state:'detached'});
  }
  assert.equal(requests.filter(p=>p==='/api/confirm').length,2);assert.deepEqual(errors,[]);
  assert.equal(await page.locator('#upload-assignment').isVisible(),false);
  const historyLink=await page.getByRole('link',{name:'终评与成绩',exact:true}).getAttribute('href');assert.ok(historyLink.includes('exam='+'e'.repeat(32)));
  await page.getByRole('button',{name:'上传下一批照片',exact:true}).click();assert.equal(await page.locator('input[type=file]').evaluate(el=>el.files.length),0);assert.equal(await groupCheck.count(),0);assert.equal(await prepare.isDisabled(),true);
  assert.equal(await page.locator('#upload-exam').inputValue(),'e'.repeat(32));await page.screenshot({path:out+'-select.png',fullPage:true});
  await page.getByRole('link',{name:'考试管理',exact:true}).click();await page.getByRole('heading',{name:'考试管理',exact:true}).waitFor();await page.screenshot({path:out+'-home.png',fullPage:true});

  const badHash='#/upload?assignment='+'a'.repeat(12)+'&exam='+'f'.repeat(32);
  await page.evaluate(hash=>location.hash=hash,badHash);await page.getByRole('button',{name:'清除无效选择'}).waitFor();assert.equal(await prepare.count(),0);
  fs.writeFileSync(out+'.json',JSON.stringify({passed:true,width:Number(width),requests,grading_input_mode:'original_photo',full_page_preview:true,crop_requests:0,errors},null,2));
 }catch(e){fs.writeFileSync(out+'-failure.txt',JSON.stringify({text:await page.locator('body').innerText(),requests,errors},null,2));await page.screenshot({path:out+'-failure.png',fullPage:true});throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
