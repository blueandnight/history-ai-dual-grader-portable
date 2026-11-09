const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const [base,out,width,draftPath]=process.argv.slice(2),draft=JSON.parse(fs.readFileSync(draftPath,'utf8'));
 const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})}),page=await browser.newPage({viewport:{width:Number(width),height:1000}}),errors=[],parses=[],creates=[];
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{const p=new URL(r.url()).pathname;if(p==='/api/exam-import-ai-parse')parses.push(r.postDataJSON());if(p==='/api/create-imported-exam')creates.push(p)});
 await page.route('**/*',r=>new URL(r.request().url()).origin===base?r.continue():r.abort());
 await page.route('**/api/import-exam-draft',r=>r.fulfill({json:{draft_id:'a'.repeat(32),draft}}));
 try{
  await page.goto(base+'/next/#/create');await page.getByLabel('Word试题',{exact:false}).setInputFiles({name:'synthetic.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:Buffer.from('mock upload intercepted')});
  await page.getByRole('button',{name:'解析为草稿',exact:true}).click();await page.getByLabel('考试名称',{exact:true}).fill('教师标题');
  await page.getByLabel('17.1参考答案',{exact:true}).fill('教师答案');await page.getByLabel('17.1满分',{exact:true}).fill('12');await page.getByLabel('17.1评分方式',{exact:true}).selectOption('point_bank');await page.getByLabel('17.1每点分值',{exact:true}).fill('3');await page.getByLabel('17.1评分点1分值',{exact:true}).fill('3');await page.getByLabel('第1题正确答案',{exact:true}).selectOption('D');
  await page.getByText('可选：AI辅助提取答案与评分标准',{exact:true}).click();
  const button=page.getByRole('button',{name:'AI 提取答案与评分标准',exact:true}),send=page.getByRole('button',{name:'确认发送文字并解析',exact:true}),check=page.getByRole('checkbox',{name:'我已核对文字来源和模型，确认原文无个人信息，同意发送文字解析',exact:true});
  await button.click();await send.waitFor();assert.equal(await send.isDisabled(),true);assert.equal(parses.length,0);await page.getByRole('button',{name:'取消AI解析',exact:true}).click();assert.equal(parses.length,0);
  const assertEdits=async()=>{assert.equal(await page.getByLabel('考试名称',{exact:true}).inputValue(),'教师标题');assert.equal(await page.getByLabel('17.1参考答案',{exact:true}).inputValue(),'教师答案');assert.equal(await page.getByLabel('17.1满分',{exact:true}).inputValue(),'12');assert.equal(await page.getByLabel('17.1评分方式',{exact:true}).inputValue(),'point_bank');assert.equal(await page.getByLabel('17.1每点分值',{exact:true}).inputValue(),'3');assert.equal(await page.getByLabel('第1题正确答案',{exact:true}).inputValue(),'D');assert.equal(await page.getByRole('checkbox',{name:'我已核对题目、答案、参考答案、满分和所有评分点',exact:true}).isChecked(),false)};
  for(let i=0;i<2;i++){await page.getByRole('checkbox',{name:'我已核对题目、答案、参考答案、满分和所有评分点',exact:true}).check();await button.click();await check.check();await send.click();await page.getByRole('status').filter({hasText:'AI识别客观题26道'}).waitFor();await page.waitForFunction(()=>!document.querySelector('fieldset')?.disabled);await assertEdits()}
  assert.equal(await page.getByLabel('第26题正确答案',{exact:true}).count(),1);assert.equal(await page.getByLabel('17.2满分',{exact:true}).inputValue(),'6');assert.equal(parses.length,2);assert.equal(creates.length,0);
  const examples=await page.getByLabel('19.1参考答案',{exact:true}).inputValue();
  assert.match(examples,/范例一[\s\S]+范例二/);assert.doesNotMatch(examples,/评分标准/);
  assert.equal(await page.getByLabel('19.1评分方式',{exact:true}).inputValue(),'holistic');
  assert.equal(await page.getByLabel('本档评价标准',{exact:true}).nth(1).inputValue(),'史实支持观点');
  for(const p of parses){assert.deepEqual(Object.keys(p).sort(),['confirm_send_text','draft_id','preview_sha256']);assert.equal(p.confirm_send_text,true);assert.ok(p.preview_sha256);assert.equal(JSON.stringify(p).includes('data:image'),false)}
  await page.route('**/api/exam-import-ai-preview',r=>r.abort());await button.click();await page.getByRole('alert').filter({hasText:'AI解析预览未完成'}).waitFor();assert.equal(parses.length,2);assert.deepEqual(errors,[]);
  await page.screenshot({path:out+'.png',fullPage:true});fs.writeFileSync(out+'.json',JSON.stringify({passed:true,width:Number(width),objective_controls:26,errors,preview_cancel_stops_parse:true,repeated_parse_preserves_edits:true,automatic_create:false,real_models:0},null,2));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
