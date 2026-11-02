const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const [base,assignment,out,width]=process.argv.slice(2);
  const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
  const page=await browser.newPage({viewport:{width:Number(width),height:900}});
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
  try {
    await page.goto(base+'/next/#/history?assignment='+assignment);
    const controls=page.getByRole('region',{name:'批改停止与继续'});
    await controls.getByRole('button',{name:'停止本批剩余批改',exact:true}).click();
    await controls.getByRole('button',{name:'确认停止剩余批改',exact:true}).click();
    await controls.getByRole('button',{name:'继续本批剩余批改',exact:true}).waitFor();
    await page.reload();
    await controls.getByRole('button',{name:'继续本批剩余批改',exact:true}).waitFor();
    await page.waitForFunction(()=>[...document.querySelectorAll('span.status-tag')].filter(e=>e.textContent==='已停止（未开始）').length===3);
    await page.screenshot({path:out+'-stopped.png',fullPage:true});
    await controls.getByRole('button',{name:'继续本批剩余批改',exact:true}).click();
    await controls.getByRole('button',{name:'确认继续批改',exact:true}).click();
    await controls.getByRole('button',{name:'停止本批剩余批改',exact:true}).waitFor();
    await page.waitForFunction(()=>[...document.querySelectorAll('span.status-tag')].filter(e=>e.textContent==='排队中').length===3);
    assert.deepEqual(errors,[]);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    console.log('PASS '+width+'px: stop -> reload persists -> explicit resume; no grades changed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
