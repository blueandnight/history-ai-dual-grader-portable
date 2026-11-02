const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('fs');
(async()=>{
 const [base,meta,photos,out]=process.argv.slice(2), saved=JSON.parse(fs.readFileSync(meta)).saved, aid=saved.assignment.id,exam=saved.exam_id;
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[],posts=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')posts.push({path:new URL(r.url()).pathname,body:r.postData()})});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 const get=path=>page.evaluate(async p=>{const r=await fetch(p);if(!r.ok)throw Error(p+':'+r.status);return r.json()},path);
 try{
  await page.goto(base+'/next/#/upload?assignment='+aid+'&exam='+exam);await page.getByLabel('用户名',{exact:true}).fill('test_teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByRole('heading',{name:'上传与批改',exact:true}).waitFor();await page.waitForFunction(e=>document.querySelector('#upload-exam').value===e,exam);
  await page.getByLabel('学生照片',{exact:false}).setInputFiles([1,2,3,4,5].map(i=>`${photos}/subjective-${i}.png`));await page.getByLabel('每份照片张数').fill('1');
  await page.getByRole('checkbox',{name:'我已核对照片顺序、每份分组和考试版式'}).check();await page.getByRole('button',{name:'核对分组并编辑原照片隐私',exact:true}).click();
  await page.getByLabel('本批隐私状态',{exact:true}).selectOption('no_identity_visible');await page.getByRole('button',{name:'应用到尚未确认的答卷',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('img[alt$="份外发图1"]').length===5||document.querySelectorAll('img[alt^="第"]').length>=5);
  await page.getByRole('checkbox',{name:'我已逐张查看实际外发图片，确认没有未排除的身份信息，并同意提交双模型批改'}).check();await page.getByRole('button',{name:'确认并上传本批',exact:true}).click();await page.getByRole('heading',{name:'上传完成',exact:true}).waitFor();
  for(let i=1;i<=5;i++){
   await page.getByRole('button',{name:`进入第${i}份教师终评`,exact:true}).waitFor({timeout:60000});
   if(i===3)continue;
   await page.getByRole('button',{name:`进入第${i}份教师终评`,exact:true}).click();const root=page.getByRole('region',{name:'教师逐点终评',exact:true});
   await root.getByRole('button',{name:'查看原图第1张',exact:true}).click();await root.getByRole('checkbox',{name:'我已打开原图并核对模型证据',exact:true}).check();
   await root.getByRole('button',{name:'记录人工复核',exact:true}).click();await root.getByRole('status').filter({hasText:'已保存原图人工复核'}).waitFor();
   const points=root.locator('select[id^="teacher-"]');assert.equal(await points.count(),4);for(let j=0;j<4;j++)await points.nth(j).selectOption(i===2?'teacher_none':'teacher_full');
   await root.getByRole('button',{name:'保存教师终评',exact:true}).click();await root.getByRole('status').filter({hasText:'教师终评已保存'}).waitFor();
   page.once('dialog',d=>d.accept());await page.getByRole('button',{name:'关闭本份终评，返回本批',exact:true}).click();await root.waitFor({state:'detached'});
  }
  const uploads=posts.filter(p=>p.path==='/api/submissions').map(p=>JSON.parse(p.body));assert.equal(uploads.length,5);const subs=uploads.map(u=>u.submission_id);
  await page.getByRole('link',{name:'终评与成绩',exact:true}).click();await page.getByRole('button',{name:'答卷关联',exact:true}).click();
  const links=page.getByRole('region',{name:'答卷关联',exact:true});await links.locator('fieldset').waitFor();
  const listing=await get('/objective/api/answer-links?exam_id='+exam);assert.equal(listing.objective_submissions.length,5);assert.equal(listing.subjective_submissions.length,5);
  const objs=listing.objective_submissions.map(x=>x.submission_id);
  // Explicit fixture manifest pairs synthetic originals; never rely on runtime auto-matching.
  const manifest=JSON.parse(fs.readFileSync(`${photos}/manifest.json`));
  for(let i=0;i<5;i++){
   const obj=listing.objective_submissions.find(x=>x.anonymous_code===manifest.objective_codes[i]);assert.ok(obj,'explicit synthetic manifest');
   const selects=links.locator('fieldset select');await selects.nth(0).selectOption(obj.submission_id);await selects.nth(1).selectOption(subs[i]);
   await links.getByRole('button',{name:'打开两边原始答卷核对'}).click();await page.waitForFunction(()=>{const imgs=[...document.querySelectorAll('section[aria-label="答卷关联"] img')];return imgs.length===2&&imgs.every(i=>i.complete&&i.naturalWidth)});
   await links.getByRole('textbox').fill(`合成验收清单 TEST-${i+1}，查看两侧原图后明确核对`);await links.getByRole('checkbox').nth(0).check();await links.getByRole('checkbox').nth(1).check();
   const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/objective/api/answer-links'&&r.request().method()==='POST');await links.getByRole('button',{name:'保存答卷关联',exact:true}).click();assert.equal((await response).status(),201);
   await page.waitForFunction(n=>document.querySelectorAll('section[aria-label="答卷关联"] button').length>=n,i+1);
   await links.getByRole('button',{name:'查看或更正关联'}).nth(i).waitFor();
  }
  await page.getByRole('button',{name:'统一成绩',exact:true}).click();const scores=page.getByRole('region',{name:'统一成绩',exact:true});await scores.getByText('69 / 69',{exact:true}).first().waitFor();
  const before=await get('/objective/api/unified-scores?exam_id='+exam);const bySub=id=>before.rows.find(r=>r.subjective?.id===id);
  assert.equal(bySub(subs[0]).total_score,69);assert.equal(bySub(subs[1]).total_score,0);assert.equal(bySub(subs[2]).total_score,null);assert.equal(bySub(subs[3]).total_score,69);assert.equal(bySub(subs[4]).total_score,null);
  assert.match(bySub(subs[2]).status,/待确认/);assert.match(bySub(subs[4]).status,/待复核/);await page.screenshot({path:out+'-before-unlink.png',fullPage:true});
  const current=await get('/objective/api/answer-links?exam_id='+exam),first=current.links.find(l=>l.subjective_submission_id===subs[0]&&l.active);
  await page.getByRole('button',{name:'答卷关联',exact:true}).click();await links.locator('fieldset').waitFor();
  const buttons=links.getByRole('button',{name:'查看或更正关联'});const index=current.links.filter(l=>l.active).findIndex(l=>l.id===first.id);await buttons.nth(index).click();await links.getByRole('textbox').fill('合成验收：解除关联，原成绩保留');await links.getByRole('button',{name:'解除关联（填写原因后）'}).nth(index).click();await page.waitForFunction(()=>document.querySelectorAll('section[aria-label="答卷关联"] button').length>0);
  await page.getByRole('button',{name:'统一成绩',exact:true}).click();await scores.getByText('69 / 69',{exact:true}).waitFor();
  const after=await get('/objective/api/unified-scores?exam_id='+exam);const detached=after.rows.filter(r=>r.objective?.id===first.objective_submission_id||r.subjective?.id===subs[0]);assert.equal(detached.length,2);assert.ok(detached.every(r=>r.total_score===null));assert.equal(detached.find(r=>r.objective).objective.score,61);assert.equal(detached.find(r=>r.subjective).subjective.score,8);
  assert.ok(posts.filter(p=>p.path==='/api/confirm').every(p=>JSON.parse(p.body).exam_id===exam));assert.deepEqual(errors,[]);fs.writeFileSync(out+'.json',JSON.stringify({passed:true,saved,manifest,subs,before,after,posts,errors},null,2));
 }catch(e){fs.writeFileSync(out+'-failure.txt',await page.locator('body').innerText());throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
