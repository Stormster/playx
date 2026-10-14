// Local-only control socket fixture: never sends votes to the live server.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const zlib = require('node:zlib');
const base = process.env.PLAYX_WEB_URL || 'http://127.0.0.1:8790/embed.html';
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYX_BROWSER_EXECUTABLE});
  try {
    for (const width of [480, 320]) {
      const context = await browser.newContext({viewport:{width,height:480}, reducedMotion:width === 320 ? 'reduce' : 'no-preference'});
      await context.addInitScript(() => {
        window.TestMode = {sent:[], control(m) {this.socket.onmessage({data:JSON.stringify(m)});}, state(share=0, voters=0, mode='democracy') {this.control({t:'state',mode,anarchyShare:share,modeVoters:voters,viewers:1,tally:{},windowMs:1500,left:1000,played:0,paused:false,idle:false});}};
        window.WebSocket = class {
          constructor() {this.readyState=1; TestMode.socket=this; setTimeout(()=>{this.onopen?.(); TestMode.control({t:'hello', audioRate:16384, video:'chunks', steps:false}); TestMode.state();},0);}
          send(data) {TestMode.sent.push(JSON.parse(data));}
          close() {}
        };
      });
      const page = await context.newPage();
      await page.goto(base);
      await page.waitForFunction(()=>document.querySelector('[data-el="watchers"]').textContent === '1 playing');
      await page.evaluate(()=>document.fonts.ready);
      const shotDir = process.env.PLAYX_BASELINE ? '/tmp/playx-mode-before' : '/tmp/playx-mode-after';
      fs.mkdirSync(shotDir,{recursive:true});
      await page.screenshot({path:`${shotDir}/${width}-no-votes.png`});
      const boxes = () => page.locator('.seg button, .seg .pct').evaluateAll(els=>els.map(el=>{const r=el.getBoundingClientRect();return [r.x,r.y,r.width,r.height];}));
      const original = await boxes();
      await page.locator('.seg button[data-mode="anarchy"]').click();
      await page.evaluate(()=>{TestMode.state(1,1,'anarchy'); TestMode.control({t:'modechange',mode:'anarchy',lockMs:30000});});
      await page.waitForTimeout(120);
      await page.screenshot({path:`${shotDir}/${width}-anarchy.png`});
      if (process.env.PLAYX_BASELINE) {console.log('BASELINE',width,JSON.stringify({before:original,after:await boxes()})); await context.close(); continue;}
      assert(await page.locator('[data-el="sound"]').evaluate(el=>el.classList.contains('nudge')));
      assert.equal(await page.locator('[data-el="soundLabel"]').innerText(),'TAP FOR SOUND');
      await page.locator('[data-el="sound"]').click();
      assert.equal(await page.locator('[data-el="sound"]').getAttribute('aria-label'),'Sound on');
      assert.equal(await page.locator('[data-el="sound"]').getAttribute('aria-pressed'),'true');
      assert.equal(await page.locator('[data-el="sound"]').evaluate(el=>el.classList.contains('nudge')),false);
      await page.locator('[data-el="sound"]').click();
      assert.equal(await page.locator('[data-el="sound"]').getAttribute('aria-label'),'Sound off');
      assert.equal(await page.evaluate(()=>localStorage.getItem('xp-audio-used')),'1');
      assert.deepEqual(await boxes(),original,'percentage updates must not move mode controls');
      assert.equal(await page.locator('[data-el="modeAnnouncement"]').innerText(),'ANARCHY TAKES OVER\nEVERY PRESS COUNTS');
      assert.match(await page.locator('[data-el="hudMode"]').innerText(),/^ANARCHY 00:2/);
      assert.deepEqual(await page.evaluate(()=>TestMode.sent.filter(m=>m.t==='mode'||m.t==='vote')),[{t:'mode',m:'anarchy'}]);
      await page.evaluate(()=>{window.modeMutations=0; new MutationObserver(ms=>window.modeMutations+=ms.length).observe(document.querySelector('[data-el="modeAnnouncement"]'),{subtree:true,childList:true,attributes:true});});
      for (const share of [0.5,0,1,0.5]) {await page.evaluate(s=>TestMode.state(s,2,'anarchy'),share); assert.deepEqual(await boxes(),original);}
      await page.evaluate(()=>TestMode.control({t:'modechange',mode:'anarchy',lockMs:30000}));
      assert.equal(await page.evaluate(()=>window.modeMutations),0,'same-mode states/events must not replay the banner');
      await page.locator('.seg button[data-mode="democracy"]').click();
      await page.locator('.seg button[data-mode="anarchy"]').click();
      assert.deepEqual(await boxes(),original);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      if(width===320) assert.equal(await page.locator('[data-el="modeAnnouncement"]').evaluate(el=>getComputedStyle(el).animationName),'none');
      // Establish real chunk playback delay, then prove mode/state never queue behind it.
      const frame = Buffer.alloc(240*160*2);
      const packet = Buffer.concat([Buffer.from([1,0,0,0,0,0,0]),zlib.deflateSync(frame)]);
      const header = Buffer.alloc(10);header.write('XPC1');header.writeUInt16LE(1,8);
      const len = Buffer.alloc(4);len.writeUInt32LE(packet.length);
      await page.route('**/fixture.bin',route=>route.fulfill({body:Buffer.concat([header,len,packet])}));
      await page.evaluate(()=>TestMode.control({t:'chunk',n:0,url:'/fixture.bin',end:true}));
      await page.waitForFunction(()=>PlayXVideo().delayMs>500);
      await page.evaluate(()=>{TestMode.control({t:'modechange',mode:'democracy',lockMs:30000}); TestMode.state(0,1,'democracy');});
      assert.match(await page.locator('[data-el="hudMode"]').innerText(),/^DEMOCRACY/);
      assert.equal(await page.locator('.seg button[data-mode="anarchy"] .pct').innerText(),'0%');
      await page.evaluate(()=>{TestMode.state(0,1,'democracy'); TestMode.control({t:'modechange',mode:'democracy',lockMs:600000}); TestMode.control({t:'modelock',lockMs:0});});
      assert.equal(await page.locator('[data-el="hudMode"]').innerText(),'DEMOCRACY');
      await page.waitForTimeout(2700);
      assert(await page.locator('[data-el="modeAnnouncement"]').isHidden());
      await page.reload();
      await page.waitForFunction(()=>document.querySelector('[data-el="watchers"]').textContent === '1 playing');
      assert.equal(await page.locator('[data-el="soundLabel"]').innerText(),'SOUND OFF');
      assert.equal(await page.locator('[data-el="sound"]').evaluate(el=>el.classList.contains('nudge')),false);
      await context.close();
      console.log(`PASS ${width}px: stable percentages, immediate mode state, one banner per transition, no game vote, no overflow`);
    }
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
