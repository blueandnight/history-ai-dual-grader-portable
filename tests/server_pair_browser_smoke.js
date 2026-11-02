const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const [base,out,width]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:950}}),errors=[],secretResponses=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('response',async r=>{if(new URL(r.url()).pathname.startsWith('/api/')){const t=await r.text().catch(()=> '');if(t.includes('SYNTHETIC_BROWSER'))secretResponses.push(r.url())}});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/system');await page.getByLabel('用户名',{exact:true}).fill('teacher-test');await page.getByLabel('密码',{exact:true}).fill('synthetic-old-password-123');await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByRole('button',{name:'展开模型配置',exact:true}).click();
  const config=page.getByRole('region',{name:'模型配置管理'}).or(page.locator('section[aria-label="模型配置管理"]'));
  await config.locator('#pair-A').selectOption('qwen35-plus');await config.getByLabel('AI-A API Key（留空沿用）',{exact:true}).fill('SYNTHETIC_BROWSER_VUE_KEY');
  assert.equal(await config.getByRole('button',{name:'保存AI-A配置',exact:true}).isDisabled(),true);await config.getByRole('checkbox',{name:'确认修改AI-A配置',exact:true}).check();await config.getByRole('button',{name:'保存AI-A配置',exact:true}).click();
  await config.getByRole('status').filter({hasText:'配置已保存'}).waitFor();await page.waitForFunction(()=>document.querySelector('#pair-A')?.value==='qwen35-plus'&&!document.querySelector('#pair-A').disabled);
  assert.equal(await config.getByLabel('AI-A API Key（留空沿用）',{exact:true}).inputValue(),'');
  await page.goto(base+'/#view-management');await page.locator('#dual-model-a-preset option[value="qwen35-plus"]').waitFor({state:'attached'});
  await page.locator('#dual-model-a-preset').selectOption('qwen36-plus');assert.equal(await page.locator('#dual-model-a-key').isEnabled(),true);
  await page.locator('#dual-model-a-key').fill('SYNTHETIC_BROWSER_LEGACY_REPLACEMENT');await page.locator('#dual-model-a-form button[type="submit"]').click();
  await page.waitForFunction(()=>document.querySelector('#dual-model-a-key')?.value===''&&document.querySelector('#dual-model-a-preset')?.value==='qwen36-plus'&&!document.querySelector('#dual-model-a-form button[type="submit"]').disabled);
  assert.equal(await page.locator('#dual-model-a-state').innerText(),'Key 已配置');await page.locator('#dual-model-a-form').screenshot({path:out+'.png'});
  assert.equal(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}).includes('SYNTHETIC_BROWSER')),false);
  const teacher=await browser.newPage();await teacher.goto(base+'/next/#/system');await teacher.getByLabel('用户名',{exact:true}).fill('test_teacher');await teacher.getByLabel('密码',{exact:true}).fill('synthetic-teacher-password-123');await teacher.getByRole('button',{name:'登录',exact:true}).click();await teacher.waitForFunction(()=>location.hash==='#/home');assert.equal(await teacher.getByRole('link',{name:'系统信息',exact:true}).count(),0);
  assert.deepEqual(errors,[]);assert.deepEqual(secretResponses,[]);fs.writeFileSync(out+'.json',JSON.stringify({passed:true,width:Number(width),secretResponses,keyInputsCleared:true}));
 }catch(e){fs.writeFileSync(out+'-error.txt',await page.locator('body').innerText());throw e}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
