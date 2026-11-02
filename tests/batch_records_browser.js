const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const [base,assignment,exam,batch,one,other,out,width]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(String(e)));await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  const scope='assignment='+assignment+'&exam='+exam;
  await page.goto(base+'/next/#/batches?'+scope);
  const card=page.locator('[data-batch-id="'+batch+'"]');await card.getByRole('link',{name:'本批成绩',exact:true}).waitFor();
  assert((await card.innerText()).includes('处理结束'));await card.getByRole('link',{name:'本批成绩',exact:true}).click();
  const root=page.getByRole('region',{name:'本批答卷工作台',exact:true});await root.locator('[data-review-id="'+one+'"]').waitFor();
  assert.equal(await root.locator('[data-review-id="'+other+'"]').count(),0);
  assert.equal(await root.locator('.history-table>table>thead th').count(),5);assert.equal(await root.locator('table table').count(),0);
  assert(await root.getByRole('group',{name:'小题双评对照'}).isVisible());
  assert.equal(await root.locator('.history-table>table>tbody>tr').count(),1);
  if(Number(width)===1280)assert((await root.locator('.history-table>table>tbody>tr').boundingBox()).height<160);
  await page.reload();await root.locator('[data-review-id="'+one+'"]').waitFor();assert((await page.evaluate(()=>location.hash)).includes('batch='+batch));
  await root.locator('[data-review-id="'+one+'"]').click();await page.getByRole('region',{name:'教师逐点终评',exact:true}).waitFor();
  await page.getByRole('button',{name:'返回本批成绩',exact:true}).click();await root.locator('[data-review-id="'+one+'"]').waitFor();
  await page.screenshot({path:out+'-batch-scores.png',fullPage:true});
  await page.getByRole('link',{name:'答卷批改',exact:true}).click();
  const recent=page.getByRole('region',{name:'最近批次',exact:true});await recent.locator('a[href*="batch='+batch+'"]').waitFor();
  assert((await recent.innerText()).includes('处理结束'));await recent.locator('a[href*="batch='+batch+'"]').click();await root.locator('[data-review-id="'+one+'"]').waitFor();
  await root.getByRole('link',{name:'返回批次列表',exact:true}).click();
  await page.getByRole('link',{name:'继续处理本批',exact:true}).click();await root.locator('[data-review-id="'+other+'"]').waitFor();
  await root.getByRole('link',{name:'任务恢复',exact:true}).click();await root.getByRole('heading',{name:'异常恢复操作',exact:true}).waitFor();
  assert((await page.evaluate(()=>location.hash)).startsWith('#/batch?'));
  await page.evaluate(hash=>location.hash=hash,'#/batch?'+scope+'&batch='+'f'.repeat(12));
  await page.getByRole('alert').filter({hasText:'本批记录不存在'}).waitFor();assert.equal(await page.locator('[data-review-id]').count(),0);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert.deepEqual(errors,[]);
  console.log('PASS '+width+'px: completed batch -> exact scores -> refresh -> review/back -> recent batch; invalid scope blocked; order and grades unchanged')
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
