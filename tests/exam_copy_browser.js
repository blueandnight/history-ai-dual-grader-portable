const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const [base,aid,out,width]=process.argv.slice(2),browser=await chromium.launch({...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{}),headless:true});
 const page=await browser.newPage({viewport:{width:Number(width),height:950}}),errors=[];page.on('pageerror',e=>errors.push(String(e)));
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/home');
  await page.locator('[data-assignment-id="'+aid+'"]').waitFor();const initialCount=await page.locator('.exam-list > li').count();
  await page.locator('[data-assignment-id="'+aid+'"]').getByRole('button',{name:'复制为新考试',exact:true}).click();
  const form=page.getByRole('region',{name:'复制为新考试',exact:true});
  await form.getByRole('textbox',{name:'新考试名称'}).fill('合成考试第二场');
  await page.screenshot({path:out+'/copy-form-'+width+'.png'});
  await form.getByRole('button',{name:'创建新考试',exact:true}).click();await form.waitFor({state:'detached'});
  const added=page.locator('.exam-list > li').filter({hasText:'合成考试第二场'});await added.waitFor();
  assert.equal(await page.locator('.exam-list > li').count(),initialCount+1);
  assert((await added.innerText()).includes('主观题 0 份 · 客观题 0 份'));
  const newId=await added.getAttribute('data-assignment-id');assert.notEqual(newId,aid);
  await page.getByRole('link',{name:'终评与成绩',exact:true}).click();
  await page.getByRole('button',{name:'统一成绩',exact:true}).click();
  await page.getByText('本考试尚无答卷',{exact:true}).waitFor();assert(page.url().includes(newId));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));assert.deepEqual(errors,[]);
  console.log('PASS '+width+'px: copy, rename, create, new context and empty independent results');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
