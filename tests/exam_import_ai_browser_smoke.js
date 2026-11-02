// Real-browser smoke for the text-only AI import parse button (synthetic DB, mocked model).
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const [base, out, width = '1280', draftPath] = process.argv.slice(2);
  const draft = JSON.parse(fs.readFileSync(draftPath, 'utf8'));
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: Number(width), height: 1300 } });
  const errors = [];
  const postBodies = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('request', request => {
    if (request.method() === 'POST') postBodies.push(request.postData() || '');
  });
  page.on('dialog', async dialog => {
    assert.match(dialog.message(), /不会发送页面图片/);
    await dialog.accept();
  });
  await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
  try {
    await page.goto(base + '/#view-exam-create', { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => typeof window.renderExamDraft === 'function', null, { timeout: 30000 });
    await page.evaluate(value => window.renderExamDraft(value, 'a'.repeat(32)), draft);
    await page.locator('#exam-ai-parse-box').waitFor({ state: 'visible', timeout: 15000 });
    await page.locator('.exam-subjective-card > summary').first().click();
    await page.locator('#exam-create-name').fill('教师修改考试名');
    await page.locator('[data-major-prompt]').first().fill('教师修改材料');
    await page.locator('[data-subq-question]').first().fill('教师修改设问');
    await page.locator('[data-subq-reference]').first().fill('教师修改参考答案');
    await page.locator('[data-subq-max-score]').first().fill('12');
    await page.locator('[data-subq-scoring-mode]').first().selectOption('point_bank');
    await page.locator('[data-subq-point-value]').first().fill('3');
    await page.locator('[data-point-label]').first().fill('教师修改评分点');
    await page.locator('[data-point-score]').first().fill('3');
    await page.locator('[data-exam-answer][data-question-id="1"]').selectOption('D');
    await page.locator('#exam-teacher-confirm').check();
    let parseCount = 0;
    await page.route('**/api/exam-import-ai-parse', async route => {
      parseCount++;
      // This edit happens after dispatch, not before clicking the button.
      if (parseCount === 1) await page.locator('[data-subq-reference]').first().fill('等待期间修改参考答案');
      await route.continue();
    });
    await page.locator('#ai-parse-exam-draft').click();
    await page.locator('#exam-ai-parse-summary').waitFor({ state: 'visible', timeout: 30000 });
    const summary = await page.locator('#exam-ai-parse-summary').innerText();
    assert.match(summary, /AI 识别 26 道/);
    assert.match(summary, /新增 10 道/);
    assert.equal(await page.locator('[data-exam-answer]').count(), 26);
    const assertEdits = async () => {
      assert.equal(await page.locator('#exam-create-name').inputValue(), '教师修改考试名');
      assert.equal(await page.locator('[data-major-prompt]').first().inputValue(), '教师修改材料');
      assert.equal(await page.locator('[data-subq-question]').first().inputValue(), '教师修改设问');
      assert.equal(await page.locator('[data-subq-reference]').first().inputValue(), '等待期间修改参考答案');
      assert.equal(await page.locator('[data-subq-max-score]').first().inputValue(), '12');
      assert.equal(await page.locator('[data-subq-scoring-mode]').first().inputValue(), 'point_bank');
      assert.equal(await page.locator('[data-subq-point-value]').first().inputValue(), '3');
      assert.equal(await page.locator('[data-point-label]').first().inputValue(), '教师修改评分点');
      assert.equal(await page.locator('[data-point-score]').first().inputValue(), '3');
      assert.equal(await page.locator('[data-exam-answer][data-question-id="1"]').inputValue(), 'D');
      assert.equal(await page.locator('#exam-teacher-confirm').isChecked(), false);
      assert.equal(await page.locator('[data-exam-subq-id="17.2"] [data-subq-max-score]').inputValue(), '6');
    };
    await assertEdits();
    const secondResponse = page.waitForResponse(response => response.url().endsWith('/api/exam-import-ai-parse') && response.status() === 200);
    await page.locator('#ai-parse-exam-draft').click();
    await secondResponse;
    await page.locator('#ai-parse-exam-draft').waitFor({ state: 'visible' });
    await page.waitForFunction(() => !document.querySelector('#ai-parse-exam-draft').disabled);
    await assertEdits();
    const warningText = await page.locator('#exam-warning-groups').innerText();
    assert.match(warningText, /AI 解析/);
    const bodies = postBodies.join('\n');
    assert.equal(bodies.includes('image_url'), false, 'AI parse request must not contain image parts');
    assert.equal(bodies.includes('data:image'), false, 'AI parse request must not contain image data');
    assert.match(bodies, /confirm_send_text/);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: out + '.png', fullPage: true });
    fs.writeFileSync(out + '.json', JSON.stringify({
      passed: true, width: Number(width), summary, objective_controls: 26, errors,
      image_parts_in_request: false,
      manual_edits_preserved: true, pending_edits_preserved: true, repeated_parse_preserved: true,
    }, null, 2));
  } catch (error) {
    await page.screenshot({ path: out + '.failure.png', fullPage: true }).catch(() => {});
    console.error(error);
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
