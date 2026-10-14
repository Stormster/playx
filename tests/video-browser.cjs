// Run against the local web/ server with Playwright available on NODE_PATH.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const zlib = require('node:zlib');

function batch(type, seq, color) {
  const frame = Buffer.alloc(240 * 160 * 2);
  for (let i = 0; i < frame.length; i += 2) frame.writeUInt16LE(color, i);
  const packet = Buffer.concat([Buffer.from([1, 0, 0]), Buffer.alloc(4), zlib.deflateSync(frame)]);
  packet.writeUInt32LE(seq, 3);
  const length = Buffer.alloc(4); length.writeUInt32LE(packet.length);
  return Buffer.concat([Buffer.from([type]), length, packet]);
}
function chunk(seq, color) {
  const packets = batch(1, seq, color).subarray(1);
  const header = Buffer.alloc(10); header.write('XPC1'); header.writeUInt16LE(1, 8);
  return Buffer.concat([header, packets]);
}
async function fixture(browser, relayUp = true) {
  const context = await browser.newContext();
  await context.addInitScript(relayUp => {
    const sockets = [];
    const control = () => sockets.findLast(s => !s.relay && s.readyState === 1);
    window.TestVideo = {
      sent: [], relayUp,
      connections() { return sockets.filter(s => !s.relay).length; },
      reconnect() { control().close(); },
      control(data) { control().onmessage({ data: JSON.stringify(data) }); },
      video(bytes) { sockets.find(s => s.relay && s.readyState === 1).onmessage({ data: Uint8Array.from(bytes).buffer }); },
      state(seq, paused=false, playing) { this.control({t:'state', seq, paused, playing, idle:paused, mode:'democracy', viewers:1, tally:{}, ticker:[], windowMs:1500, left:1000, played:0}); }
    };
    window.WebSocket = class {
      constructor(url) {
        this.relay = url.includes('/relay'); this.readyState = 1; sockets.push(this);
        setTimeout(() => {
          this.onopen?.();
          if (!this.relay) {
            window.TestVideo.control({t:'hello', audioRate:16384, steps:false, video:'chunks', relay:'ws://127.0.0.1:8789/relay', relayUp:window.TestVideo.relayUp});
            window.TestVideo.state(42);
          }
        }, 0);
      }
      send(data) { if (!this.relay) window.TestVideo.sent.push(JSON.parse(data)); }
      close() { this.readyState = 3; setTimeout(() => this.onclose?.(), 0); }
    };
  }, relayUp);
  const page = await context.newPage();
  await page.goto(process.env.PLAYX_WEB_URL || 'http://127.0.0.1:8789');
  await page.waitForFunction(() => !!window.PlayXVideo && window.TestVideo.sent.some(m => m.t === 'hello'));
  if (relayUp) await page.waitForFunction(() => window.PlayXVideo().relayConnected);
  return { context, page };
}
(async () => {
  const browser = await chromium.launch({ headless:true, ...(process.env.PLAYX_BROWSER_EXECUTABLE ? {executablePath:process.env.PLAYX_BROWSER_EXECUTABLE} : {}) });
  try {
    {
      const {context, page} = await fixture(browser, false);
      await page.route('**/red.bin', route => route.fulfill({body:chunk(100, 0xf800)}));
      await page.route('**/green.bin', route => route.fulfill({body:chunk(0, 0x07e0)}));
      await page.route('**/blue.bin', route => route.fulfill({body:chunk(0, 0x001f)}));
      await page.evaluate(() => TestVideo.control({t:'chunk', n:100, url:'/red.bin', end:true}));
      await page.waitForFunction(() => document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data[0] > 200);
      await page.evaluate(() => TestVideo.reconnect());
      await page.waitForFunction(() => TestVideo.sent.filter(m => m.t === 'hello').length === 2);
      await page.evaluate(() => TestVideo.control({t:'chunk', n:0, url:'/green.bin', end:true}));
      await page.waitForFunction(() => document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data[1] > 200);
      await page.evaluate(() => TestVideo.reconnect());
      await page.waitForFunction(() => TestVideo.sent.filter(m => m.t === 'hello').length === 3);
      await page.evaluate(() => TestVideo.control({t:'chunk', n:0, url:'/blue.bin', end:true}));
      await page.waitForFunction(() => document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data[2] > 200);
      await context.close();
      console.log('PASS reconnect accepts restarted chunk numbers and replays the same frozen chunk');
    }
    {
      const {context, page} = await fixture(browser, false);
      let release, started;
      const requested = new Promise(resolve => started = resolve);
      const gate = new Promise(resolve => release = resolve);
      await page.route('**/old-restart.bin', async route => { started(); await gate; await route.fulfill({body:chunk(100, 0xf800)}); });
      await page.route('**/new-restart.bin', route => route.fulfill({body:chunk(0, 0x07e0)}));
      await page.evaluate(() => TestVideo.control({t:'chunk', n:100, url:'/old-restart.bin', end:true}));
      await requested;
      await page.evaluate(() => TestVideo.reconnect());
      await page.waitForFunction(() => TestVideo.sent.filter(m => m.t === 'hello').length === 2);
      await page.evaluate(() => TestVideo.control({t:'chunk', n:0, url:'/new-restart.bin', end:true}));
      await page.waitForFunction(() => document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data[1] > 200);
      release();
      await page.waitForTimeout(800);
      const pixel = await page.evaluate(() => [...document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data]);
      assert(pixel[1] > 200 && pixel[0] < 30, JSON.stringify(pixel));
      assert.equal(await page.evaluate(() => PlayXVideo().queued), 0);
      await context.close();
      console.log('PASS stale fetch from previous connection cannot block or overwrite restarted playback');
    }
    {
      const {context, page} = await fixture(browser);
      await page.evaluate(() => {
        const original = Response.prototype.arrayBuffer;
        let held = false;
        Response.prototype.arrayBuffer = async function() {
          const bytes = await original.call(this);
          if (!held) {
            held = true;
            TestVideo.decodeHeld = true;
            await new Promise(resolve => TestVideo.releaseDecode = resolve);
          }
          return bytes;
        };
      });
      await page.evaluate(bytes => TestVideo.video(bytes), [...batch(4, 100, 0xf800)]);
      await page.waitForFunction(() => TestVideo.decodeHeld);
      await page.evaluate(() => { TestVideo.relayUp = false; TestVideo.reconnect(); });
      await page.waitForFunction(() => TestVideo.sent.filter(m => m.t === 'hello').length === 2);
      await page.route('**/new-decode.bin', route => route.fulfill({body:chunk(0, 0x07e0)}));
      await page.evaluate(() => TestVideo.control({t:'chunk', n:0, url:'/new-decode.bin', end:true}));
      await page.waitForFunction(() => document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data[1] > 200);
      await page.evaluate(() => TestVideo.releaseDecode());
      await page.waitForTimeout(100);
      const pixel = await page.evaluate(() => [...document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data]);
      assert(pixel[1] > 200 && pixel[0] < 30, JSON.stringify(pixel));
      await context.close();
      console.log('PASS stale decode cannot block or overwrite the new connection');
    }
    {
      const {context, page} = await fixture(browser);
      await page.evaluate(bytes => TestVideo.video(bytes), [...batch(4, 100, 0xf800)]);
      await page.waitForFunction(() => document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data[0] > 200);
      await page.evaluate(() => TestVideo.reconnect());
      await page.waitForFunction(() => TestVideo.sent.filter(m => m.t === 'hello').length === 2 && PlayXVideo().relayConnected);
      await page.evaluate(bytes => TestVideo.video(bytes), [...batch(4, 0, 0x07e0)]);
      await page.waitForFunction(() => document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data[1] > 200);
      assert.equal(await page.evaluate(() => PlayXVideo().source), 'relay');
      await context.close();
      console.log('PASS control reconnect establishes a fresh relay and renders its frozen picture');
    }
    {
      const {context, page} = await fixture(browser);
      await page.locator('[data-btn="up"]').click();
      const votes = await page.evaluate(() => TestVideo.sent.filter(m => m.t === 'vote'));
      assert.deepEqual(votes, [{t:'vote', b:'up', n:1}]);
      await context.close();
      console.log('PASS one on-screen pad tap sends one single-step vote');
    }
    {
      const {context, page} = await fixture(browser);
      await page.waitForFunction(() => window.PlayXVideo().onFallback, null, {timeout:6000});
      assert(await page.evaluate(() => TestVideo.sent.some(m => m.t === 'fallback')));
      await page.waitForFunction(() => window.PlayXVideo().relayConnected);
      await page.evaluate(bytes => TestVideo.video(bytes), [...batch(4, 42, 0x07e0)]);
      await page.waitForFunction(() => window.PlayXVideo().source === 'relay');
      assert(await page.evaluate(() => TestVideo.sent.some(m => m.t === 'relayok')));
      await context.close();
      console.log('PASS first-message timeout, fallback report, automatic recovery');
    }
    {
      const {context, page} = await fixture(browser);
      await page.evaluate(bytes => TestVideo.video(bytes), [...batch(2, 42, 0x07e0)]);
      await page.evaluate(() => { TestVideo.video([3]); TestVideo.state(43); });
      await page.waitForFunction(() => window.PlayXVideo().onFallback, null, {timeout:6000});
      await context.close();
      console.log('PASS frozen stream resumes on server but stalled relay falls back');
    }
    {
      const {context, page} = await fixture(browser);
      await page.evaluate(bytes => TestVideo.video(bytes), [...batch(2, 42, 0x07e0)]);
      await page.evaluate(() => TestVideo.state(43, true, true));
      await page.waitForFunction(() => window.PlayXVideo().onFallback, null, {timeout:6000});
      await context.close();
      console.log('PASS clock-idle animation still detects relay stalls');
    }
    {
      const {context, page} = await fixture(browser);
      let release, started;
      const requested = new Promise(resolve => started = resolve);
      const gate = new Promise(resolve => release = resolve);
      await page.route('**/old.bin', async route => { started(); await gate; await route.fulfill({body:chunk(10, 0xf800)}); });
      await page.evaluate(() => TestVideo.control({t:'chunk', n:0, url:'/old.bin', end:true}));
      await requested;
      await page.evaluate(bytes => TestVideo.video(bytes), [...batch(4, 42, 0x07e0)]);
      await page.waitForFunction(() => document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data[1] > 200);
      release();
      await page.waitForTimeout(700);
      const result = await page.evaluate(() => ({video:PlayXVideo(), pixel:[...document.querySelector('canvas').getContext('2d').getImageData(0,0,1,1).data]}));
      assert.equal(result.video.source, 'relay');
      assert.equal(result.video.queued, 0);
      assert(result.pixel[1] > 200 && result.pixel[0] < 30, JSON.stringify(result));
      await context.close();
      console.log('PASS stale chunk fetch cannot overwrite recovered frozen relay picture');
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
