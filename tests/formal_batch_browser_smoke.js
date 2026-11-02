// Run via formal_batch_browser_smoke.py with synthetic data and a mock provider.
const { chromium } = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const [base, file, output, width = '1280'] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: Number(width), height: 1000 } });
  const errors = [], requests = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('dialog', async dialog => { errors.push(dialog.message()); await dialog.dismiss(); });
  page.on('request', req => { if (req.method() === 'POST') requests.push(new URL(req.url()).pathname); });
  await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
  try {
    await page.goto(base);
    await page.locator('#batch-assignment-select option[value="aaaaaaaaaaaa"]').waitFor({ state: 'attached' });
    await page.click('#mode-batch');
    await page.selectOption('#batch-assignment-select', 'aaaaaaaaaaaa');
    await page.waitForFunction(() => document.querySelector('#batch-exam-select').value === 'e'.repeat(32));
    await page.setInputFiles('#batch-photos-album', file);
    await page.locator('#batch-form button[type="submit"]').click();
    await page.getByRole('button', { name: '确认分组并开始批改', exact: true }).click();
    await page.locator('.formal-crop-checked').first().waitFor({ timeout: 30000 });
    assert.equal(await page.locator('.formal-crop-checked').count(), 2);
    assert.equal(await page.locator('.batch-ai-review-dialog').evaluate(el => el.scrollWidth > el.clientWidth + 2), false);
    await page.screenshot({ path: output + '.crops.png', fullPage: true });
    assert.equal(await page.locator('.formal-crop-accept').isDisabled(), true);
    assert.equal(requests.includes('/api/submissions'), false);
    await page.locator('.formal-crop-checked').nth(0).check();
    assert.equal(await page.locator('.formal-crop-accept').isDisabled(), true);
    await page.locator('.formal-crop-checked').nth(1).check();
    await page.locator('.formal-crop-accept').click();
    await page.locator('.privacy-status-select').waitFor({ timeout: 15000 });
    assert.equal(await page.locator('.batch-ai-outbound-image').count(), 2);
    assert.equal(await page.locator('.batch-ai-confirm-send').isDisabled(), true);
    assert.equal(requests.includes('/api/submissions'), false);
    await page.selectOption('.privacy-status-select', 'no_identity_visible');
    assert.equal(await page.locator('.batch-ai-confirm-send').isDisabled(), true);
    await page.locator('.batch-ai-consent').check();
    await page.locator('.batch-ai-confirm-send').click();
    await page.locator('tr[data-review-pending="true"]').last().waitFor({ timeout: 30000 });
    assert.equal(requests.filter(x => x === '/api/batches').length, 1);
    assert.equal(requests.filter(x => x === '/api/submissions').length, 1);
    const batchAt = requests.indexOf('/api/batches');
    assert.ok(requests.indexOf('/api/student-auto-crop-review') < batchAt);
    assert.ok(requests.indexOf('/api/ai-payload-preview') < batchAt);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: output + '.png', fullPage: true });
    fs.writeFileSync(output + '.json', JSON.stringify({ passed: true, width: Number(width), requests, errors, crop_confirmation: true, privacy_confirmation: true }, null, 2));
  } catch (e) {
    await page.screenshot({ path: output + '.failure.png', fullPage: true });
    console.error(errors); throw e;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
