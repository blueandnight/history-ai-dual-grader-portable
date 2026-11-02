const {chromium}=require('playwright');const assert=require('node:assert/strict');
(async()=>{
 const [base,out,width]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
 const page=await browser.newPage({viewport:{width:Number(width),height:950},acceptDownloads:true}),errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/system');await page.getByRole('button',{name:'展开模型配置',exact:true}).click();
  const config=page.getByRole('region',{name:'模型配置管理'});
  await page.getByLabel('主模型预设',{exact:true}).selectOption('custom');
  await config.getByLabel('接口地址',{exact:true}).fill('https://example.invalid/v1');
  await config.getByLabel('主模型API Key（留空沿用现有）',{exact:true}).fill('SYNTHETIC_TYPED');
  await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname==='/api/custom-models/list'),config.getByRole('button',{name:'确认查询模型列表'}).click()]);
  await config.getByLabel('上游模型列表',{exact:true}).selectOption('synthetic-vision');
  assert.equal(await config.getByLabel('主模型API Key（留空沿用现有）',{exact:true}).inputValue(),'');
  assert.equal(await config.locator('img').count(),0);
  await config.getByLabel('自定义预设名称',{exact:true}).fill('合成自定义视觉模型');
  await config.getByRole('checkbox',{name:'确认保存自定义预设',exact:true}).check();
  await config.getByRole('button',{name:'保存自定义预设',exact:true}).click();
  await page.getByLabel('AI-A模型',{exact:true}).locator('option').filter({hasText:'合成自定义视觉模型'}).waitFor({state:'attached'});
  assert.equal(await page.getByLabel('AI-B模型',{exact:true}).locator('option').filter({hasText:'合成自定义视觉模型'}).count(),1);
  await page.getByRole('button',{name:'展开本机运维',exact:true}).click();
  const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('link',{name:'导出诊断包',exact:true}).click()]);await download.saveAs(out+'-diagnostics.zip');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.screenshot({path:out+'-vue.png',fullPage:true});
  await page.goto(base+'/');await page.waitForFunction(()=>typeof fetchCustomModelList==='function'&&document.querySelector('#model-preset')?.options.length>1&&window.HISTORY_GRADER_AUTH?.required===false);
  await page.evaluate(()=>{document.querySelector('#model-base-url').value='https://example.invalid/v1';document.querySelector('#api-key').value='SYNTHETIC_TYPED';return fetchCustomModelList()});
  assert.equal(await page.locator('#custom-model-list option').count(),3);assert.equal(await page.locator('#api-key').inputValue(),'');
  assert.equal(await page.locator('#custom-model-list img').count(),0);
  assert.equal(await page.locator('a[href="/api/diagnostics-export"]').getAttribute('href'),'/api/diagnostics-export');
  assert.deepEqual(errors,[]);console.log('Model/support UI PASS '+width);
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
