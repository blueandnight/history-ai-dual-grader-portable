const {chromium}=require('playwright');const assert=require('node:assert/strict');const fs=require('node:fs');
(async()=>{
 const [base,aid,exam,out,width]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:1000}});
 const errors=[],writes=[];page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()!=='GET')writes.push(new URL(r.url()).pathname)});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/history?assignment='+aid+'&exam='+exam);await page.getByLabel('用户名',{exact:true}).fill('test_teacher');await page.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByRole('button',{name:'统一成绩',exact:true}).click();
  const root=page.getByRole('region',{name:'统一成绩',exact:true});await root.getByText('68 / 68',{exact:true}).waitFor();assert.equal(await root.locator('tbody tr').count(),3);
  const complete=root.locator('tbody tr').filter({hasText:'匿名01'}),pending=root.locator('tbody tr').filter({hasText:'匿名02'}),unlinked=root.locator('tbody tr').filter({hasText:'匿名03'});
  assert.match(await complete.innerText(),/已完整确认/);assert.match(await pending.innerText(),/主观题待确认/);assert.match(await pending.innerText(),/尚未完整确认/);assert.match(await unlinked.innerText(),/未关联/);
  assert.equal(await root.locator('input').count(),0);assert.doesNotMatch(await root.innerText(),/DO_NOT_EXPOSE_NAME/);
  await root.getByRole('button',{name:'刷新统一成绩',exact:true}).click();await root.getByText('68 / 68',{exact:true}).waitFor();
  await complete.getByRole('button',{name:'主观题详情',exact:true}).click();await root.getByRole('region',{name:'答卷只读详情',exact:true}).waitFor();
  await complete.getByRole('button',{name:'教师逐点终评',exact:true}).click();await root.getByRole('heading',{name:/教师逐点终评/}).waitFor();
  await page.screenshot({path:out+'.png',fullPage:true});
  await complete.getByRole('link',{name:'客观题详情与复核',exact:true}).click();await page.frameLocator('iframe').locator('#results .result-card').first().waitFor();assert.match(await page.frameLocator('iframe').locator('#results').innerText(),/60 \/ 60/);assert.equal(await page.frameLocator('iframe').locator('#independent-key').isVisible(),false);
  assert.deepEqual(errors,[]);assert.deepEqual(writes.filter(p=>!p.includes('/auth/')),[]);fs.writeFileSync(out+'.json',JSON.stringify({passed:true,complete:68,pending:true,unlinked:true,anonymous:true,errors,writes},null,2));
 }catch(e){fs.writeFileSync(out+'-failure.txt',await page.locator('body').innerText());throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
