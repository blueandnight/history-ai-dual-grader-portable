const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const [base,assignment,exam,out,width,fail]=process.argv.slice(2);
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:950}}),errors=[],writes=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')writes.push(r.url())});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/history?assignment='+assignment+'&exam='+exam);
  const root=page.getByRole('region',{name:'双评成绩与批量采纳'});
  await root.getByRole('button',{name:'集中核对空白（2份）',exact:true}).click();
  const panel=page.getByRole('region',{name:'集中核对疑似空白'}),save=panel.getByRole('button',{name:'保存所选空白题核对'});
  await panel.locator('article').first().waitFor();assert(await save.isDisabled());
  for(const box of await panel.locator('article input').all())assert(await box.isDisabled());
  await panel.getByRole('button',{name:'选中已打开原图的小题'}).click();assert.equal(await panel.locator('article input:checked').count(),0);assert.deepEqual(writes,[]);
  await panel.getByRole('button',{name:'展开本组原图'}).click();
  for(const img of await panel.locator('img').all()){await img.scrollIntoViewIfNeeded();await img.evaluate(img=>img.complete&&img.naturalWidth>0?true:new Promise((ok,bad)=>{img.onload=ok;img.onerror=bad}));}
  await panel.getByRole('button',{name:'选中已打开原图的小题'}).click();assert.equal(await panel.locator('article input:checked').count(),2);assert(await save.isDisabled());
  await panel.getByRole('checkbox',{name:/我已逐张核对/}).check();assert(await save.isEnabled());
  await page.screenshot({path:out+'-before-save.png',fullPage:true});
  if(fail){let n=0;await page.route('**/api/confirm-blank-subquestion',r=>++n===2?r.abort():r.continue())}
  await save.click();if(fail){await panel.getByRole('alert').filter({hasText:'后续已停止'}).waitFor();assert(await save.isDisabled());await page.waitForTimeout(500);assert.equal(writes.length,2);assert.deepEqual(errors,[]);console.log('PASS failure: one saved, next uncertain, further writes stopped; no automatic retry');return;}await panel.getByRole('status').filter({hasText:'已记录 2 道'}).waitFor({timeout:5000}).catch(async e=>{throw new Error(await panel.innerText()+' / '+writes.join(','))});
  assert.equal(writes.length,2);assert(writes.every(u=>u.endsWith('/api/confirm-blank-subquestion')));
  await panel.getByRole('button',{name:'关闭集中核对'}).click();
  await root.getByRole('combobox').selectOption('blank');await root.getByText('该分组没有答卷，可切换查看全部。').waitFor();
  await root.getByRole('combobox').selectOption('all');
  assert((await root.innerText()).includes('空白已核对，待终评'));assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:out+'-after-save.png',fullPage:true});assert.deepEqual(errors,[]);
  console.log('PASS '+width+'px: real blank-review API, image/selection/consent gates, two sequential writes, raw AI and final grades preserved; models 0');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
