const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const [base,aid,exam,out,width]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:+width,height:1000}}),errors=[];
 page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(String(e)));
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 const shot=async name=>{await page.screenshot({path:`${out}/${name}-${width}.png`});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true)};
 try{
  await page.goto(base+'/next/#/home?assignment='+aid+'&exam='+exam);
  await page.getByLabel('用户名',{exact:true}).fill('test_teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.locator('.exam-list li').filter({hasNotText:'Unrelated'}).getByRole('button',{name:'删除考试',exact:true}).click();
  const deletion=page.getByRole('region',{name:'删除考试确认'}),name=await page.locator('.exam-list li').filter({hasNotText:'Unrelated'}).locator('strong').innerText();
  await deletion.getByText('将一并取消2份尚未执行的主观题任务，并移除1个未完成批次。',{exact:true}).waitFor();await deletion.getByLabel('输入完整考试名称确认').fill('wrong');assert.equal(await deletion.getByRole('button',{name:'永久删除考试',exact:true}).isDisabled(),true);
  await deletion.getByRole('button',{name:'取消',exact:true}).click();assert.equal(await page.locator('.exam-list li').count(),2);
  await page.goto(base+'/next/#/history?assignment='+aid+'&exam='+exam);const cleanup=page.locator('.photo-cleanup');await cleanup.locator('summary').click();
  await cleanup.getByText('· 未终评，可弃用清理',{exact:true}).waitFor();await cleanup.getByRole('button',{name:'选择全部可清理答卷'}).click();assert.equal(await cleanup.locator('input[type=checkbox]:checked').count(),3);assert.equal(await cleanup.locator('input[type=checkbox]:disabled').count(),2);
  const consent=cleanup.getByLabel('我已核对所选答卷，确认不再需要原图。',{exact:false});await consent.check();await shot('cleanup-confirm');
  await cleanup.getByRole('button',{name:'清理所选照片',exact:true}).click();await cleanup.getByText('匿名03：已清理照片，成绩保留',{exact:true}).waitFor();assert.equal(await cleanup.getByRole('listitem').count(),3);
  await page.getByRole('button',{name:'刷新历史',exact:true}).click();await page.locator('tr').filter({hasText:'匿名01'}).getByRole('cell',{name:'0/8',exact:true}).waitFor();
  // A denied network response on a newly allowed synthetic pending record must stop before the next item.
  await page.route('**/api/assignment-submissions?*',async r=>{const response=await r.fetch(),d=await response.json();for(const s of d.submissions){s.photos=[{}];s.photos_deleted=false;s.confirmed_at='synthetic';s.teacher_reconfirmation_required=false}await r.fulfill({response,json:d})});
  await page.getByRole('button',{name:'刷新历史',exact:true}).click();await cleanup.locator('summary').click();await cleanup.getByRole('button',{name:'选择全部可清理答卷'}).click();await consent.check();
  let attempts=0;await page.route('**/api/delete-submission-photos',r=>{attempts++;return r.abort('failed')});await cleanup.getByRole('button',{name:'清理所选照片',exact:true}).click();await cleanup.getByRole('alert').waitFor();assert.equal(attempts,1);assert.equal(await cleanup.getByRole('button',{name:'清理所选照片',exact:true}).isDisabled(),true);
  await page.unroute('**/api/delete-submission-photos');await page.unroute('**/api/assignment-submissions?*');
  await page.goto(base+'/next/#/home?assignment='+aid+'&exam='+exam);await page.locator('.exam-list li').filter({hasNotText:'Unrelated'}).getByRole('button',{name:'删除考试',exact:true}).click();await deletion.getByLabel('输入完整考试名称确认').fill(name);await shot('delete-confirm');
  await deletion.getByRole('button',{name:'永久删除考试',exact:true}).click();await page.getByRole('status').filter({hasText:'已删除'}).waitFor();await page.waitForFunction(()=>document.querySelectorAll('.exam-list li').length===1);assert.equal(await page.getByLabel('当前考试',{exact:true}).inputValue(),'');
  await page.reload();await page.getByRole('heading',{name:'考试管理',exact:true}).waitFor();assert.equal(await page.locator('.exam-list li').count(),1);assert.equal(await page.getByRole('button',{name:'归档考试',exact:true}).count(),0);
  assert.deepEqual(errors,[]);fs.writeFileSync(`${out}/audit-${width}.json`,JSON.stringify({passed:true,width:+width,attemptsAfterLostResponse:attempts,errors}));
 }catch(e){fs.writeFileSync(`${out}/failure-${width}.txt`,await page.locator('body').innerText());throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
