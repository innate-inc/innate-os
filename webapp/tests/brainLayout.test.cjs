// Run with Playwright installed: node webapp/tests/brainLayout.test.cjs
// Uses the real Agent entry point and synthetic Brain demo, with an inert ROS
// socket. No robot connection or motion is involved.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname;
  const file = path.join(root, path.extname(pathname) ? pathname : 'index.html');
  if (!file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
  fs.readFile(file, (error, data) => {
    if (error) { res.writeHead(404).end(); return; }
    const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };
    res.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream');
    res.end(data);
  });
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || 'chrome' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.routeWebSocket('**/ws', () => {});
    const url = `http://127.0.0.1:${server.address().port}`;
    await page.goto(url);
    await page.getByRole('button', { name: 'Inspect brain activity' }).click();
    await page.locator('.br-stage-idle').waitFor({ state: 'visible' });
    assert.equal(await page.locator('.br-stage-idle').innerText(), 'NO SIGNAL');
    await page.getByRole('button', { name: 'Back to live camera' }).click();
    await page.goto(url + '/brain?demo');
    await page.locator('.br-thumb').first().waitFor();
    for (const [width, height] of [[1440, 900], [1366, 768], [1280, 720], [1024, 768], [820, 900], [390, 844]]) {
      await page.setViewportSize({ width, height });
      await page.waitForTimeout(150);
      assert(await page.locator('.agent-brain').isVisible(), 'resize must preserve inspection');
      const layout = await page.evaluate(() => {
        const brain = document.querySelector('.agent-brain');
        const stage = document.querySelector('.br-stage').getBoundingClientRect();
        const grid = document.querySelector('.br-grid').getBoundingClientRect();
        const dock = document.querySelector('.agent-panel').getBoundingClientRect();
        return { overflow: brain.scrollWidth > brain.clientWidth, stageHeight: stage.height, gridRight: grid.right, dockLeft: dock.left };
      });
      assert(!layout.overflow, `${width}: no horizontal overflow`);
      assert(layout.stageHeight >= 260, `${width}: camera remains large (${layout.stageHeight})`);
      if (width > 820) assert(layout.gridRight <= layout.dockLeft, `${width}: monitor clears chat`);
      await page.locator('.br-thumb').nth(1).click();
      assert.match(await page.locator('.br-frame-cap').innerText(), /wrist camera/);
      await page.locator('.br-thumb').first().click();
      await page.locator('.br-inspect-btn').click();
      assert(await page.locator('.br-inspect').isVisible());
      await page.keyboard.press('Escape');
      assert(!(await page.locator('.br-inspect').isVisible()));
      await page.locator('.br-panel-vitals').scrollIntoViewIfNeeded();
      assert(await page.locator('.br-panel-vitals').isVisible());
      assert(await page.locator('.agent-brain').isVisible());
      await page.getByRole('button', { name: 'Back to live camera' }).click();
      assert(!(await page.locator('.agent-brain').isVisible()));
      await page.getByRole('button', { name: 'Inspect brain activity' }).click();
      await page.locator('.agent-brain').evaluate(el => el.scrollTop = 0);
      if (process.env.SCREENSHOT_DIR) {
        fs.mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true });
        await page.screenshot({ animations: 'disabled', path: path.join(process.env.SCREENSHOT_DIR, `brain-${width}.png`) });
      }
      console.log(`ok - ${width}×${height}: camera, switching, inspector, resize, return`);
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
