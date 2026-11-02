// Real-browser smoke for same-page objective + subjective layout (synthetic DB, mock-free).
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const [base, out, width = '1280', mode = 'full'] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const viewportHeight = mode === 'full' ? 1800 : 1000;
  const page = await browser.newPage({ viewport: { width: Number(width), height: viewportHeight } });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
  async function drag(bbox) {
    const box = await page.locator('#exam-layout-reference-image').boundingBox();
    const x1 = box.x + box.width * bbox.x1;
    const y1 = box.y + box.height * bbox.y1;
    const x2 = box.x + box.width * bbox.x2;
    const y2 = box.y + box.height * bbox.y2;
    await page.mouse.move(x1, y1);
    await page.mouse.down();
    await page.mouse.move(x2, y2, { steps: 6 });
    await page.mouse.up();
  }
  try {
    await page.goto(base, { waitUntil: 'domcontentloaded' });
    const openButton = page.locator('#assignment-history .open-exam-layout-profile');
    await openButton.waitFor({ state: 'attached', timeout: 30000 });
    await page.evaluate(() => {
      document.querySelector('#main-tab-history')?.click();
      document.querySelector('button[data-app-view-target="history"]')?.click();
    });
    await openButton.waitFor({ state: 'visible', timeout: 15000 });
    await openButton.click();
    await page.locator('#exam-layout-profile-workspace').waitFor({ state: 'visible', timeout: 30000 });
    await page.locator('#exam-layout-profile-objective').waitFor({ state: 'visible', timeout: 15000 });
    assert.equal(await page.locator('#exam-layout-profile-page option').count(), 2, 'references are pages 3 and 4');
    await page.selectOption('#exam-layout-profile-page', '4');
    await page.locator('.exam-layout-canvas-scroll').evaluate(element => { element.scrollTop = 0; });
    await page.locator('#exam-layout-reference-frame').scrollIntoViewIfNeeded();

    if (mode === 'full') {
      await page.selectOption('#exam-layout-objective-start', '1');
      await page.selectOption('#exam-layout-objective-end', '10');
      await page.locator('#exam-layout-objective-draw').click();
      await drag({ x1: .18, y1: .10, x2: .92, y2: .16 });
      await page.locator('.exam-layout-region-box').filter({ hasText: '客观 1–10' }).waitFor({ timeout: 15000 });
      assert.match(await page.locator('#exam-layout-profile-progress').innerText(), /客观题 10 \/ 16/);

      await page.selectOption('#exam-layout-objective-start', '11');
      await page.selectOption('#exam-layout-objective-end', '16');
      await page.locator('#exam-layout-objective-draw').click();
      await drag({ x1: .18, y1: .17, x2: .67, y2: .23 });
      await page.locator('.exam-layout-region-box').filter({ hasText: '客观 11–16' }).waitFor({ timeout: 15000 });
      assert.match(await page.locator('#exam-layout-profile-progress').innerText(), /客观题 16 \/ 16/);
      assert.equal(await page.locator('#exam-layout-objective-list .exam-layout-objective-item').count(), 2);

      await page.selectOption('#exam-layout-profile-page', '3');
      await page.locator('.exam-layout-canvas-scroll').evaluate(element => { element.scrollTop = 0; });
      await page.locator('.exam-layout-question[data-question-id="17.1"]').click();
      await drag({ x1: .08, y1: .67, x2: .92, y2: .81 });
      await page.locator('.exam-layout-region-box').filter({ hasText: '17.1' }).waitFor({ timeout: 15000 });
      assert.match(await page.locator('#exam-layout-profile-progress').innerText(), /已配置 1 \/ 5 个主观小题/);

      const saveButton = page.locator('#save-exam-layout-profile');
      assert.equal(await saveButton.isDisabled(), false);
      const [response] = await Promise.all([
        page.waitForResponse(item => item.url().startsWith(base + '/api/exam-layout-profile') && item.request().method() === 'POST'),
        saveButton.click(),
      ]);
      assert.equal(response.status(), 200);
      assert.match(await page.locator('#exam-layout-profile-summary').innerText(), /客观题答题区/);
      await page.screenshot({ path: out + '.png', fullPage: true });

      await page.locator('#close-exam-layout-profile').click();
      await page.locator('#assignment-history .open-exam-layout-profile').click();
      await page.locator('#exam-layout-profile-workspace').waitFor({ state: 'visible' });
      await page.locator('#exam-layout-objective-list').getByText('第 1–10 题').waitFor();
      await page.locator('#exam-layout-objective-list').getByText('第 11–16 题').waitFor();
      await page.screenshot({ path: out + '-reloaded.png', fullPage: true });
    } else {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 2), true, 'no horizontal overflow at 390px');
      await page.locator('#exam-layout-profile-objective').screenshot({ path: out + '.png' });
    }

    assert.deepEqual(errors, []);
    fs.writeFileSync(out + '.json', JSON.stringify({ passed: true, mode, width: Number(width), errors }, null, 2));
  } catch (error) {
    await page.screenshot({ path: out + '.failure.png', fullPage: true }).catch(() => {});
    console.error(error);
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

