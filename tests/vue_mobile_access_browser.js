const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const [base,file,out,width]=process.argv.slice(2),browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:1000}});
 const errors=[],paths=[],posts=[];page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{paths.push(new URL(r.url()).pathname);if(r.method()==='POST')posts.push(new URL(r.url()).pathname)});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 try{
  await page.goto(base+'/next/#/upload?assignment='+'a'.repeat(12)+'&exam='+'e'.repeat(32));await page.getByRole('heading',{name:'上传与批改',exact:true}).waitFor();
  await page.getByLabel('学生照片',{exact:false}).setInputFiles(file);const url=page.url();
  await page.getByRole('button',{name:'手机拍照与连接',exact:true}).click();const panel=page.getByRole('region',{name:'手机拍照与连接',exact:true});await panel.getByText('合成 Wi-Fi',{exact:true}).waitFor();assert.equal(page.url(),url);assert.deepEqual(posts,[]);
  const generate=panel.getByRole('button',{name:'生成手机访问码',exact:true});assert.equal(await generate.isDisabled(),true);await panel.getByRole('checkbox',{name:'确认生成临时手机访问码（如有旧码将失效）',exact:true}).check();await generate.click();await panel.getByRole('button',{name:'复制手机地址',exact:true}).waitFor();
  const text=await panel.locator('code').innerText();assert.ok(text.startsWith('http://192.0.2.10:'));assert.ok(text.includes('access_token='));
  await panel.getByRole('button',{name:'撤销手机访问码',exact:true}).click();await panel.getByRole('status').filter({hasText:'手机访问码已撤销'}).waitFor();assert.equal((await panel.locator('code').innerText()).includes('access_token='),false);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await panel.screenshot({path:out+'.png'});await panel.getByRole('button',{name:'收起手机连接',exact:true}).click();assert.equal(page.url(),url);assert.equal(await page.getByLabel('学生照片',{exact:false}).evaluate(el=>el.files.length),1);
  page.once('dialog',d=>d.accept());await page.getByRole('link',{name:'系统管理',exact:true}).click();await page.getByRole('button',{name:'展开本机运维',exact:true}).click();const shared=page.getByRole('region',{name:'本机运维管理',exact:true}).getByRole('region',{name:'手机拍照与连接',exact:true});await shared.getByText('合成 Wi-Fi',{exact:true}).waitFor();await shared.getByRole('checkbox',{name:'确认生成临时手机访问码（如有旧码将失效）',exact:true}).check();await shared.getByRole('button',{name:'生成手机访问码',exact:true}).click();await shared.getByRole('button',{name:'复制手机地址',exact:true}).waitFor();await shared.getByRole('button',{name:'撤销手机访问码',exact:true}).click();await shared.getByRole('status').filter({hasText:'手机访问码已撤销'}).waitFor();
  assert.equal(paths.some(p=>p.startsWith('/legacy')),false);assert.deepEqual(posts,['/api/mobile-token','/api/mobile-token/revoke','/api/mobile-token','/api/mobile-token/revoke']);assert.deepEqual(errors,[]);fs.writeFileSync(out+'.json',JSON.stringify({passed:true,width:Number(width),same_page:true,files_preserved:true,legacy_navigation:false,posts,errors},null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
