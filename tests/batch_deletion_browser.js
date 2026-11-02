const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const [base,assignment,exam,batch,other,out,width,lost]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:1000}}),writes=[],errors=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(r.method()==='POST')writes.push(r.url())});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 if(lost)await page.route('**/api/delete-batch',async r=>{const response=await r.fetch();assert.equal(response.status(),200);await r.abort()});
 try{
  await page.goto(base+'/next/#/batches?assignment='+assignment+'&exam='+exam);
  const row=id=>page.locator('tr[data-batch-id="'+id+'"]');await row(batch).waitFor();await row(other).waitFor();
  await row(batch).getByRole('button',{name:'删除本批次',exact:true}).click();
  const panel=page.getByRole('region',{name:'删除批次确认'}),remove=panel.getByRole('button',{name:'永久删除本批次',exact:true});
  await panel.getByRole('checkbox').waitFor();assert(await remove.isDisabled());assert.deepEqual(writes,[]);
  assert((await panel.innerText()).includes('已有确认成绩 1 份'));assert((await panel.innerText()).includes('客观答卷与成绩'));
  await panel.getByRole('checkbox').check();assert(await remove.isEnabled());
  await page.screenshot({path:out+'-delete-confirm.png',fullPage:true});await remove.click();
  if(lost){await panel.getByRole('alert').filter({hasText:'删除结果未能确认'}).waitFor();assert(await remove.isDisabled());await page.waitForTimeout(500);assert.equal(writes.length,1);await panel.getByRole('button',{name:'取消',exact:true}).click();await page.getByRole('button',{name:'刷新批次',exact:true}).click();}
  await row(batch).waitFor({state:'detached'});await row(other).waitFor();
  assert.deepEqual(writes,[base+'/api/delete-batch']);assert.deepEqual(errors,[]);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  console.log('PASS '+width+'px'+(lost?' lost response':'')+': explicit preview/consent, one POST, only chosen batch removed, other objective/subjective records and files preserved; models 0');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
