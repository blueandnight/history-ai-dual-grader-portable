const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const [base,manifest,out,width]=process.argv.slice(2),m=JSON.parse(fs.readFileSync(manifest,'utf8'));
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:950}}),errors=[],requests=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>requests.push(new URL(r.url()).pathname));
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/history?assignment='+m.assignment+'&exam='+m.exam+'&panel=scores');
  const scores=page.getByRole('region',{name:'统一成绩',exact:true});await scores.getByText('客观题有效 3 份',{exact:true}).waitFor();
  const row=scores.locator('tbody tr').filter({hasText:m.subject});await row.getByRole('button',{name:'确认最终总分',exact:true}).click();
  const review=page.getByRole('region',{name:'快捷总分终评'});await review.getByText('满分：',{exact:false}).waitFor();
  await review.getByLabel('教师最终确认分数',{exact:true}).fill('0');await review.getByLabel('我已查看原卷并确认本次总分').check();
  await review.getByRole('button',{name:'保存最终总分并继续'}).click();await review.waitFor({state:'detached'});
  await page.getByRole('button',{name:'答卷关联（可选）',exact:true}).click();
  const links=page.getByRole('region',{name:'答卷关联',exact:true});await links.getByLabel('客观题答卷',{exact:true}).selectOption(m.objective);await links.getByLabel('主观题答卷',{exact:true}).selectOption(m.subject);
  assert.equal(await links.locator('input[type=checkbox]').count(),0);await links.locator('img').nth(1).waitFor();
  assert(await links.locator('img').evaluateAll(imgs=>imgs.every(i=>i.complete&&i.naturalWidth>0)));
  await page.screenshot({path:out+'/link-'+width+'.png'});
  await links.getByRole('button',{name:'确认这两份属于同一考生并关联'}).click();await links.getByRole('status').filter({hasText:'关联已保存'}).waitFor();
  await page.getByRole('button',{name:'统一成绩',exact:true}).click();await scores.getByText('整卷完整 2 份',{exact:true}).waitFor();
  assert((await scores.innerText()).includes('0 / 8'));assert.equal(await scores.locator('.unified-grades tbody > tr').count(),4);
  for(const side of ['objective','subjective','all']){
   await scores.getByLabel('导出内容',{exact:true}).selectOption(side);const download=page.waitForEvent('download');await scores.getByRole('link',{name:'导出 Excel',exact:true}).click();await (await download).saveAs(out+'/'+side+'-'+width+'.xlsx');
  }
  await page.screenshot({path:out+'/grades-'+width+'.png'});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.goto(base+'/next/#/objective?assignment='+m.assignment+'&exam='+m.exam+'&assessment='+m.key);
  const frame=page.frameLocator('iframe');await frame.locator('#results tbody > tr').nth(2).waitFor();assert.equal(await frame.locator('#results tbody > tr').count(),3);
  assert((await frame.locator('#exam-summary').innerText()).includes('当前版本有效 3 份'));
  assert(!(await frame.locator('body').innerText()).includes('身份码'));
  assert(!requests.some(p=>/\/api\/(identities|roster-identity|class-upload-preview|assignment-markings)/.test(p)));
  assert.deepEqual(errors,[]);console.log('PASS '+width+'px: total-only zero confirmation, thumbnail/manual pair, 2 complete + 2 unlinked rows, 3 Excel choices, 3-job objective list, no identity calls or paid models');
 }catch(e){fs.writeFileSync(out+'/failure-'+width+'.txt',await page.locator('body').innerText());await page.screenshot({path:out+'/failure-'+width+'.png'});throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
