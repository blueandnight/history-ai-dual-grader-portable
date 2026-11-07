// Synthetic UI smoke: real Vue build, mocked HTTP responses, captured create payload.
const {chromium}=require('playwright');
const fs=require('node:fs');const assert=require('node:assert/strict');
(async()=>{
 const [base,fixture,out,width]=process.argv.slice(2),draft=JSON.parse(fs.readFileSync(fixture,'utf8'));
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 const page=await browser.newPage({viewport:{width:Number(width),height:960}});const errors=[];let submitted;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**',async route=>{
  const path=new URL(route.request().url()).pathname;let body={};
  if(path==='/api/auth/session')body={required:false,authenticated:true,role:'admin'};
  else if(path==='/api/assignments')body={assignments:submitted?[{id:'c'.repeat(12),name:'合成考试',exam_id:'b'.repeat(32)}]:[],classes:[]};
  else if(path==='/api/exam-layout-profile')body={exam_id:'b'.repeat(32),assignment_id:'c'.repeat(12)};
  else if(path==='/api/import-exam-draft')body={draft_id:'a'.repeat(32),draft};
  else if(path==='/api/create-imported-exam'){
   submitted=route.request().postDataJSON();body={exam_id:'b'.repeat(32),assignment:{id:'c'.repeat(12),name:'合成考试',objective_count:0,subjective_count:3}};
  }
  await route.fulfill({json:body});
 });
 await page.goto(base+'/next/#/create');await page.getByRole('heading',{name:'新建考试',exact:true}).waitFor();
 await page.locator('input[type=file]').first().setInputFiles({name:'synthetic.docx',mimeType:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',buffer:Buffer.from('synthetic-upload')});
 await page.getByRole('button',{name:'解析为草稿',exact:true}).click();
 await page.getByLabel('大题编号',{exact:true}).first().fill('21');await page.getByLabel('大题编号',{exact:true}).first().press('Tab');
 await page.getByRole('button',{name:'添加小题',exact:true}).first().click();
 await page.getByRole('button',{name:'删除小题',exact:true}).nth(1).click();
 const sub=page.locator('[data-question="21.1"]');
 await sub.getByRole('combobox').filter({has:page.locator('option[value="holistic"]')}).selectOption('holistic');
 await sub.getByRole('button',{name:'添加档次',exact:true}).click();
 await sub.getByLabel('最低分',{exact:true}).fill('0');await sub.getByLabel('最高分',{exact:true}).fill('12');await sub.getByLabel('本档评价标准',{exact:true}).fill('合成综合评价标准：观点、史实、论证与逻辑。');
 await sub.getByLabel('评价维度',{exact:true}).fill('观点、史实、论证、逻辑');
 await page.locator('input[name=teacher-confirmed]').check();
 await page.getByText('实时预览（当前编辑内容）',{exact:true}).click();
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+3),'page overflow');
 await page.screenshot({path:out+'.png',fullPage:true});
 assert(await page.getByRole('button',{name:'确认创建考试',exact:true}).isEnabled());
 await page.getByRole('button',{name:'确认创建考试',exact:true}).click();
 await page.waitForTimeout(500);console.log(JSON.stringify({submitted:!!submitted,invalid:await page.locator(':invalid').evaluateAll(nodes=>nodes.map(n=>({tag:n.tagName,name:n.name,id:n.id,value:n.value}))),alerts:await page.locator('[role=alert]').allTextContents()}));
 await page.getByRole('heading',{name:'考试已创建',exact:true}).waitFor({timeout:3000});
 assert(submitted.confirmation.material_questions[0].sub_questions[0].question_id==='21.1');
 assert(submitted.confirmation.material_questions[0].sub_questions[0].scoring_mode==='holistic');
 assert.deepEqual(errors,[]);fs.writeFileSync(out+'.json',JSON.stringify(submitted,null,2));
 console.log(JSON.stringify({width:Number(width),created:true,errors:0}));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
