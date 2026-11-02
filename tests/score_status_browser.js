const {chromium}=require('playwright');
const assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const [base,assignment,exam,idsFile,out,width]=process.argv.slice(2),ids=JSON.parse(fs.readFileSync(idsFile,'utf8'));
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:1000}}),errors=[],writes=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')writes.push(r.url())});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/history?assignment='+assignment+'&exam='+exam);
  const root=page.getByRole('region',{name:'双评成绩与批量采纳'});
  await root.getByRole('button',{name:'处理待复核答卷（2份）',exact:true}).waitFor();
  const row=k=>root.locator('.history-table>table>tbody>tr').filter({has:page.locator('[data-review-id="'+ids[k]+'"]')});
  assert((await row('blank').innerText()).includes('待确认'));assert((await root.innerText()).includes('* 为部分小题合计'));
  assert((await row('blank').innerText()).includes('0*'));
  assert(!(await row('blank').innerText()).includes('同分，评分点判断不同'));
  assert((await row('filtered').innerText()).includes('MiMo内容过滤'));await row('filtered').locator('summary').click();assert((await row('filtered').innerText()).includes('服务商内容过滤'));
  assert((await row('filtered').innerText()).includes('MiMo内容过滤'));
  for(const [key,label] of [['paused','批次暂停等待'],['queued','排队中'],['recovery','待人工恢复'],['grading','正在评分']])assert.equal(await row(key).locator('.status-tag').innerText(),label);
  assert(await row('grading').locator('[data-review-id]').isDisabled());assert(await row('recovery').locator('[data-review-id]').isDisabled());
  assert(await row('blank').getByRole('group',{name:'小题双评对照'}).isVisible());
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:out+'-statuses.png',fullPage:true});
  await row('paused').getByRole('link',{name:'任务恢复',exact:true}).click();
  assert((await page.evaluate(()=>location.hash)).startsWith('#/batches?'));assert((await page.evaluate(()=>location.hash)).includes('exam='+exam));
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  console.log('PASS '+width+'px: blank/filtered/paused/queued/grading/recovery, real readonly API, original scores unchanged, models 0');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
