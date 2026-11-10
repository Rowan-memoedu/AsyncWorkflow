// Isolated headless Chrome; never attaches to the user's browser profile.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('D:/Caches/RecordingViewBrowser/node_modules/playwright');

(async () => {
  const access = JSON.parse(fs.readFileSync('D:/AppData/RecordingView/access.json', 'utf8'));
  const output = 'D:/Artifacts/AsyncWorkflow/recording-view-20261101';
  const candidates = ['C:/Users/fy/AppData/Local/Google/Chrome/Application/chrome.exe', 'C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'];
  const executablePath = candidates.find(x => fs.existsSync(x));
  assert(executablePath, 'Installed Chrome required');
  const browser = await chromium.launch({ executablePath, headless: true });
  const results = [];
  try {
    for (const width of [320, 375, 414, 768, 1440]) {
      const context = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: 'dark',
        httpCredentials: { username: 'desktop', password: access.desktop, origin: new URL(access.url).origin } });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(access.url);
      await page.waitForFunction(() => Number(document.getElementById('total').textContent) > 0);
      assert.equal(await page.locator('#location').textContent(), '云端');
      assert.match(await page.locator('#processed').textContent(), /最近处理：\d/);
      await page.evaluate(() => { window.__listNode = document.querySelector('.recording'); window.scrollTo(0, 150); });
      const position = await page.evaluate(() => scrollY);
      await Promise.all([page.waitForResponse(r => r.url().includes('/api/recordings?')), page.locator('#refresh').evaluate(x => x.click())]);
      await page.waitForFunction(() => !document.getElementById('refresh').disabled);
      assert(await page.evaluate(() => window.__listNode === document.querySelector('.recording')), 'Refresh must preserve unchanged list nodes');
      assert.equal(await page.evaluate(() => scrollY), position, 'Refresh must preserve vertical position');
      await page.evaluate(() => window.scrollTo(0, 0));
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
      assert.equal(overflow, false, `Horizontal overflow at ${width}`);
      await page.screenshot({ path: path.join(output, `web-${width}.png`), fullPage: true });
      await page.locator('.recording').first().click();
      await page.locator('.detail-top').waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (width < 960) {
        await page.locator('.back').click();
        assert.equal(await page.locator('body').evaluate(x => x.classList.contains('detail-open')), false);
      }
      await page.locator('#search').fill('不存在的独立验证文本qvr14');
      await page.waitForFunction(() => document.querySelector('#count').textContent.includes('找到 0 条'));
      await page.locator('#search').fill('');
      await page.waitForFunction(() => document.querySelectorAll('.recording').length > 0);
      assert(new URL(page.url()).pathname === '/recordings/');
      if (width === 375) {
        await page.route('**/api/recordings?*', async route => {
          const response = await route.fetch();
          const data = await response.json();
          data.synced_at = '2020-01-01T00:00:00+00:00';
          await route.fulfill({ response, json: data });
        });
        await page.locator('#refresh').click();
        await page.waitForFunction(() => document.getElementById('error').textContent.includes('最后一次同步'));
        assert(await page.locator('.recording').count() > 0);
      }
      assert.deepEqual(errors, []);
      results.push({ width, overflow: false, authenticated: true, navigation: true, search: true });
      console.log(`PASS authenticated web at ${width}px`);
      await context.close();
    }
    fs.writeFileSync(path.join(output, 'web-verification.json'), JSON.stringify(results, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
