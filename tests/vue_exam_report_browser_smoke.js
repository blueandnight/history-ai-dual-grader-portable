const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const [base,out,width]=process.argv.slice(2), browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
 const context=await browser.newContext({viewport:{width:Number(width),height:900},acceptDownloads:true});
 const page=await context.newPage(), errors=[];let aiCalls=0, allowAI=false;
 page.on('pageerror',e=>errors.push(String(e)));
 page.on('dialog',d=>allowAI?d.accept():d.dismiss());
 await page.route('**/*',route=>new URL(route.request().url()).origin===base?route.continue():route.abort());
 await page.route('**/api/exam-report/ai',route=>{
   aiCalls++;assert.equal(route.request().postDataJSON().confirm_send,true);
   return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({report:{assignment_id:'a'.repeat(12),assignment_name:'合成卷',student_count:1,average:2,max_total:4},markdown:'# 合成分析 <img src=x>',model:'mock'})});
 });
 try {
   await page.goto(base+'/next/#/history');
   await page.getByLabel('选择考试',{exact:true}).selectOption('a'.repeat(12));
   await page.getByRole('button',{name:'试卷报告',exact:true}).click();
   const report=page.getByRole('region',{name:'试卷分析报告'});
   await report.getByRole('button',{name:'下载报告',exact:true}).waitFor();
   assert.equal(aiCalls,0);
   const [download]=await Promise.all([page.waitForEvent('download'),report.getByRole('button',{name:'下载报告',exact:true}).click()]);
   await download.saveAs(out+'-vue.md');
   await report.getByRole('button',{name:'生成 AI 分析',exact:true}).click();assert.equal(aiCalls,0);
   allowAI=true;await report.getByRole('button',{name:'生成 AI 分析',exact:true}).click();
   await report.locator('pre.analysis-text').filter({hasText:'合成分析'}).waitFor();
   assert.equal(aiCalls,1);assert.equal(await report.locator('img').count(),0);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:out+'-vue.png',fullPage:true});
   await report.getByRole('button',{name:'关闭报告'}).click();
   await page.getByRole('button',{name:'试卷报告',exact:true}).click();
   await report.getByRole('button',{name:'下载报告',exact:true}).waitFor();
   assert.equal(await report.locator('pre.analysis-text').count(),0);
   // A late AI response must not reopen a closed report or restore its analysis.
   let release;
   await page.route('**/api/exam-report/ai',async route=>{await new Promise(resolve=>release=resolve);await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({report:{assignment_id:'a'.repeat(12)},markdown:'STALE_AI'})});});
   await report.getByRole('button',{name:'生成 AI 分析',exact:true}).click();
   await page.getByText('AI 分析中…',{exact:true}).waitFor();
   await report.getByRole('button',{name:'关闭报告'}).click();release();
   await page.getByRole('button',{name:'试卷报告',exact:true}).click();
   await report.getByRole('button',{name:'下载报告',exact:true}).waitFor();
   assert.equal((await report.innerText()).includes('STALE_AI'),false);
   await page.unroute('**/api/exam-report/ai');
   await page.goto(base+'/');
   await page.waitForFunction(()=>typeof openExamReport==='function');
   await page.evaluate(()=>openExamReport('a'.repeat(12)));
   await page.locator('#download-exam-report:enabled').waitFor();
   const [oldDownload]=await Promise.all([page.waitForEvent('download'),page.locator('#download-exam-report').click()]);
   await oldDownload.saveAs(out+'-legacy.md');
   assert.equal((await page.locator('#exam-report-body').innerText()).includes('2 / 4'),true);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:out+'-legacy.png',fullPage:true});
   await page.locator('#close-exam-report').click();
   assert.deepEqual(errors,[]);console.log('report UI PASS '+width);
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
