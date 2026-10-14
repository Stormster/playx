// Disposable geometry fixture: external traffic is blocked and every socket is replaced.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.PLAYX_WEB_URL || 'http://127.0.0.1:8789';
const shots = process.env.PLAYX_LAYOUT_SHOTS || '/tmp/playx-layout-preview';
const previewFrame = process.env.PLAYX_PREVIEW_FRAME ? 'data:image/png;base64,'+fs.readFileSync(process.env.PLAYX_PREVIEW_FRAME).toString('base64') : null;
const previewFont = process.env.PLAYX_PREVIEW_FONT ? 'data:font/ttf;base64,'+fs.readFileSync(process.env.PLAYX_PREVIEW_FONT).toString('base64') : null;
const themes = ['flame', 'cobalt', 'graphite', 'pearl', 'lime'];
const sizes = [[1440,900], [844,390], [667,375], [390,844], [320,568]];
async function prepareFont(page) {
  if (previewFont) await page.addStyleTag({content:`@font-face {font-family:"Press Start 2P";src:url("${previewFont}") format("truetype");font-weight:400;font-style:normal;font-display:block;}`});
  await page.evaluate(()=>document.fonts.ready);
}
async function drawCapturedFrame(page) {
  if (previewFrame) await page.locator('canvas').evaluate(async (el,src)=> {
    const frame=new Image(); frame.src=src; await frame.decode();
    const ctx=el.getContext('2d'); ctx.imageSmoothingEnabled=false; ctx.drawImage(frame,0,0,240,160);
  },previewFrame);
}
async function assertFooterFits(page,label) {
  const g=await page.evaluate(()=> {
    const box=el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
    return {width:innerWidth,height:innerHeight,canvas:box(document.querySelector('canvas')),footer:[...document.querySelectorAll('.xp-foot a,.xp-foot button,.bezel-clock')].map(box).filter(r=>r.width>0)};
  });
  const overlaps=(a,b)=>a.x<b.right-.5&&a.right>b.x+.5&&a.y<b.bottom-.5&&a.bottom>b.y+.5;
  assert(g.footer.every(r=>r.x>=0&&r.y>=0&&r.right<=g.width&&r.bottom<=g.height),`${label}: complete footer remains in viewport`);
  assert(g.footer.every(r=>!overlaps(r,g.canvas)),`${label}: footer never covers the game`);
  for(let i=0;i<g.footer.length;i++) for(let j=i+1;j<g.footer.length;j++) assert(!overlaps(g.footer[i],g.footer[j]),`${label}: footer controls do not overlap`);
}
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.PLAYX_BROWSER_EXECUTABLE});
  fs.mkdirSync(shots, {recursive:true});
  try {
    for (const [width,height] of sizes) {
      const context = await browser.newContext({viewport:{width,height}, reducedMotion:'reduce'});
      await context.addInitScript(() => {
        window.TestLayout = {sent:[], frameAge:0, control(m) {this.socket.onmessage({data:JSON.stringify(m)});}, state(mode='democracy', share=0, played=0, simulatedPlayers=0, viewers=12) {this.control({t:'state',mode,anarchyShare:share,modeVoters:2,viewers,simulatedPlayers,tally:{},windowMs:1500,left:1000,played,paused:false,idle:false});}};
        const requestFrame=window.requestAnimationFrame.bind(window);
        window.requestAnimationFrame=callback=>requestFrame(now=>callback(now-TestLayout.frameAge));
        window.WebSocket = class {
          constructor() {this.readyState=1; TestLayout.socket=this; setTimeout(()=>{this.onopen?.(); TestLayout.control({t:'hello',audioRate:16384,video:'chunks',steps:false}); TestLayout.state();},0);}
          send(data) {TestLayout.sent.push(JSON.parse(data));}
          close() {}
        };
      });
      await context.route('**/*', route => new URL(route.request().url()).origin === new URL(base).origin ? route.continue() : route.abort());
      const page = await context.newPage();
      await page.goto(new URL('/play.html', base).href);
      await page.waitForFunction(()=>document.querySelector('[data-el="watchers"]').textContent==='12 playing');
      await prepareFont(page);
      // A fresh state can be newer than the timestamp passed to an animation callback.
      await page.evaluate(async()=> {
        TestLayout.frameAge=1000; TestLayout.state();
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      });
      assert.equal(await page.locator('[data-el="clock"]').innerText(),'RUN 00:00:00','a fresh run never shows a negative day or shifts the footer');
      await page.evaluate(()=>{TestLayout.frameAge=0; TestLayout.state();});
      const geometry = () => page.evaluate(() => {
        const box = el => {const r=el.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
        const canvas = box(document.querySelector('canvas'));
        const controls = [...document.querySelectorAll('.vbtn')].map(el=>({button:el.dataset.btn,round:el.classList.contains('round'),faceWidth:el.offsetWidth,...box(el)}));
        const top = [...document.querySelectorAll('.xp-live, .sound-btn, .seg button')].map(el=>({control:el.dataset.el||el.dataset.mode,...box(el)}));
        const footer = [...document.querySelectorAll('.xp-foot a, .bezel-clock, .keyboard-toggle, .fullscreen-toggle')].map(box);
        return {canvas,controls,top,footer,shell:box(document.querySelector('.xp')),deck:box(document.querySelector('.base')),header:box(document.querySelector('.xp-top')),scroll:[document.documentElement.scrollWidth,document.documentElement.scrollHeight],viewport:[innerWidth,innerHeight]};
      });
      const overlaps = (a,b) => a.x < b.right-.5 && a.right > b.x+.5 && a.y < b.bottom-.5 && a.bottom > b.y+.5;
      const inViewport = r => r.width>0 && r.height>0 && r.x>=-.5 && r.y>=-.5 && r.right<=width+.5 && r.bottom<=height+.5;
      for (const theme of themes) {
        await page.locator('.xp').evaluate((el,theme)=>el.dataset.theme=theme,theme);
        await page.locator('.seg [data-mode="anarchy"]').click();
        await page.evaluate(()=>TestLayout.state('democracy',.5));
        const g = await geometry();
        const label = `${width}x${height} ${theme}`;
        assert.equal(g.controls.length,10,`${label}: all ten game buttons`);
        assert(g.scroll[0]<=width && g.scroll[1]<=height,`${label}: no document overflow ${JSON.stringify(g)}`);
        assert(Math.abs(g.canvas.width/g.canvas.height-1.5)<.01,`${label}: 3:2 canvas`);
        assert(g.canvas.width>=230 && g.canvas.height>=150,`${label}: useful screen size`);
        assert(inViewport(g.canvas),`${label}: screen in viewport`);
        if(height>width) {
          assert(g.deck.height>=height*.32&&g.deck.height<=height*.39,`${label}: portrait deck occupies roughly 35% of the viewport`);
          assert(g.canvas.width>=width*.94,`${label}: portrait game uses nearly the full width`);
          const buttons=await page.locator('.vbtn').evaluateAll(els=>els.map(el=>({button:el.dataset.btn,width:el.offsetWidth,y:el.getBoundingClientRect().y})));
          assert(buttons.filter(b=>['up','down','left','right'].includes(b.button)).every(b=>b.width>=40),`${label}: larger portrait Dpad`);
          assert(buttons.filter(b=>['a','b'].includes(b.button)).every(b=>b.width>=(width>=360?60:54)),`${label}: larger portrait A/B`);
          assert(buttons.find(b=>b.button==='select').y>buttons.find(b=>b.button==='down').y+40,`${label}: Select/Start sit below main controls`);
        }
        for (const r of [...g.controls,...g.top,...g.footer]) {
          assert(inViewport(r),`${label}: control in viewport ${JSON.stringify(r)}`);
          assert(!overlaps(r,g.canvas),`${label}: controls outside game screen ${JSON.stringify(r)}`);
        }
        for (let i=0;i<g.controls.length;i++) for (let j=i+1;j<g.controls.length;j++) {
          const a=g.controls[i], b=g.controls[j];
          // Rotated circular A/B faces have intersecting bounding boxes, but disjoint circles.
          const touching=a.round&&b.round ? Math.hypot(a.x+a.width/2-b.x-b.width/2,a.y+a.height/2-b.y-b.height/2)<(a.faceWidth+b.faceWidth)/2-.5 : overlaps(a,b);
          assert(!touching,`${label}: game controls do not overlap ${a.button}/${b.button}`);
        }
        for (let i=0;i<g.top.length;i++) for (let j=i+1;j<g.top.length;j++) {
          assert(!overlaps(g.top[i],g.top[j]),`${label}: top-bar cells do not overlap`);
        }
        const sound = g.top.find(r=>r.control==='sound');
        assert(sound.y>=g.header.y && sound.bottom<=g.header.bottom,`${label}: sound is in top bar`);
        assert(await page.locator('.sound-btn').evaluate(el=>el.previousElementSibling?.classList.contains('xp-live')),`${label}: sound beside watchers`);
        assert.equal(await page.locator('.seg [aria-pressed="true"]').getAttribute('data-mode'),'anarchy');
        assert.equal(await page.locator('.seg .live').getAttribute('data-mode'),'democracy');
        const styles = await page.locator('.seg').evaluate(el=> {
          const live=el.querySelector('.live'), selected=el.querySelector('[aria-pressed="true"]');
          const probe=document.createElement('span'); probe.style.color='var(--vote)'; el.append(probe);
          const vote=getComputedStyle(probe).color; probe.remove();
          return {selected:getComputedStyle(selected).backgroundColor,live:getComputedStyle(live).backgroundColor,activeBorder:getComputedStyle(live).borderBottomColor,vote,marker:getComputedStyle(live,'::after').content,radius:getComputedStyle(selected).borderRadius};
        });
        assert.notEqual(styles.selected,styles.live,`${label}: preference and live mode have distinct fills`);
        assert.equal(styles.activeBorder,styles.vote,`${label}: active underline matches the theme vote color`);
        assert.equal(styles.marker,'none',`${label}: no extra mode label or spacer`);
        assert.equal(styles.radius,'2px',`${label}: rectangular mode cells`);
        await page.evaluate(()=>TestLayout.state('anarchy',1));
        assert.equal(await page.locator('.seg .live').getAttribute('aria-pressed'),'true',`${label}: selected and active together`);
        assert.deepEqual(await geometry(),g,`${label}: state changes keep geometry stable`);
      }
      await page.evaluate(()=>TestLayout.control({t:'hello',audioRate:16384,steps:false,viewers:2,simulatedPlayers:5}));
      assert.equal(await page.locator('[data-el="watcherCount"]').innerText(),'7 playing','initial hello includes real viewers in the combined count');
      await page.evaluate(()=>TestLayout.state('anarchy',1,0,5,2));
      assert.equal(await page.locator('[data-el="watcherCount"]').innerText(),'7 playing','public count includes real viewers and simulated players');
      assert.equal(await page.locator('[data-el="watchers"]').innerText(),'7 playing','no separate bot count is shown');
      assert.equal(await page.locator('[data-el="watchers"]').getAttribute('aria-label'),'7 playing','accessible label matches the visible count');
      for(const [bots,viewers] of [[5,2],[50,123456]]) {
        await page.evaluate(([bots,viewers])=>TestLayout.state('anarchy',1,0,bots,viewers),[bots,viewers]);
        const g=await geometry();
        assert(g.top.every(inViewport),`${width}x${height}: simulated-player header fits`);
        for(let i=0;i<g.top.length;i++) for(let j=i+1;j<g.top.length;j++) assert(!overlaps(g.top[i],g.top[j]),'bot count cannot displace the sound or mode buttons');
      }
      await page.evaluate(()=>TestLayout.state('democracy',.5));
      assert.equal(await page.locator('[data-el="watchers"]').innerText(),'12 playing','ordinary watcher label is retained');
      if(width===1440) {
        const vote=(tally,mine,left=1000)=>page.evaluate(([tally,mine,left])=>TestLayout.control({t:'state',mode:'democracy',anarchyShare:0,modeVoters:2,viewers:12,tally,mine,windowMs:1500,left,played:0,paused:false,idle:false}),[tally,mine,left]);
        const count=b=>page.locator(`.vbtn[data-btn="${b}"] .cnt`).innerText();
        await vote({a:1},null);
        await page.locator('.vbtn[data-btn="a"]').click();
        assert.equal(await count('a'),'2','my vote adds to a button someone else picked');
        await vote({a:1},null);
        assert.equal(await count('a'),'2','a state sent before my vote arrived keeps my vote added');
        await vote({a:2},'a:1');
        assert.equal(await count('a'),'2','the counted vote is not added twice');
        await page.locator('.vbtn[data-btn="b"]').click();
        assert.deepEqual([await count('a'),await count('b')],['1','1'],'switching moves my vote');
        await vote({a:1,b:1},'b:1');
        assert.deepEqual([await count('a'),await count('b')],['1','1'],'switch confirmed by the server');
        await vote({},null);
        assert.equal(await count('b'),'','a new window starts empty');
        await vote({a:1},null,100);
        await page.locator('.vbtn[data-btn="a"]').click();
        assert.equal(await count('a'),'2','late vote shows at once');
        await vote({},null,1500);
        assert.equal(await count('a'),'','a vote counted in the closing window is not carried into the next');
        assert.deepEqual(await page.evaluate(()=>TestLayout.sent.filter(m=>m.t==='vote').map(m=>m.b)),['a','b','a'],'one vote per click');
        await page.evaluate(()=>{TestLayout.sent=TestLayout.sent.filter(m=>m.t!=='vote');});
      }
      await page.locator('.xp').evaluate(el=>el.dataset.theme='flame');
      await page.evaluate(()=>TestLayout.state('democracy',.5));
      // A clearly labelled synthetic canvas makes screen scaling visible in review artifacts.
      await page.locator('canvas').evaluate(el=> {
        const ctx=el.getContext('2d');
        ctx.fillStyle='#c5daa2'; ctx.fillRect(0,0,240,160);
        for(let y=0;y<160;y+=16) for(let x=0;x<240;x+=16) {ctx.fillStyle=(x+y)%32?'#a9c18d':'#b6ce99';ctx.fillRect(x+1,y+1,14,14);}
        ctx.fillStyle='#263c35';ctx.fillRect(24,56,192,48);
        ctx.fillStyle='#f4f4cf';ctx.font='10px monospace';ctx.textAlign='center';ctx.fillText('LOCAL LAYOUT PREVIEW',120,76);ctx.fillText('240 × 160 · 3:2',120,92);
      });
      await drawCapturedFrame(page);
      if (previewFrame) await page.evaluate(()=> {
        TestLayout.state('anarchy',1);
        document.querySelector('[data-el="watcherCount"]').textContent='2 playing';
      });
      await page.screenshot({path:path.join(shots,`play-${width}x${height}.png`)});
      if(previewFrame) console.log(`PREVIEW play ${width}x${height}: canvas ${JSON.stringify(await page.locator('canvas').boundingBox())}`);
      const iconState = () => page.locator('[data-el="sound"]').evaluate(el=>({wave:getComputedStyle(el.querySelector('.sound-wave')).visibility,mute:getComputedStyle(el.querySelector('.sound-mute')).visibility}));
      assert.deepEqual(await iconState(),{wave:'hidden',mute:'visible'},'muted sound has a visible cross');
      await page.locator('[data-el="sound"]').click();
      assert.deepEqual(await iconState(),{wave:'visible',mute:'hidden'},'enabled sound shows waves and removes the cross');
      await page.locator('[data-el="sound"]').click();
      assert.deepEqual(await iconState(),{wave:'hidden',mute:'visible'},'muting restores the crossed speaker');
      await page.locator('[data-el="keyboardHelp"]').click();
      assert(await page.locator('#keyboard-guide').isVisible());
      assert(inViewport(await page.locator('#keyboard-guide').evaluate(el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};})),`${width}x${height}: keyboard guide fits`);
      await page.locator('[data-el="keyboardClose"]').click();
      // Exercise Chromium's real Fullscreen API in addition to expanded-page geometry.
      await page.locator('[data-el="fullscreen"]').click();
      await page.waitForFunction(()=>document.fullscreenElement===document.documentElement);
      assert.equal(await page.locator('[data-el="fullscreen"]').innerText(),'Exit full screen');
      assert.equal(await page.locator('[data-el="fullscreen"]').getAttribute('aria-pressed'),'true');
      assert(await page.locator('canvas').evaluate(el=>document.activeElement===el),'fullscreen entry focuses the game');
      await page.locator('[data-el="fullscreen"]').click();
      await page.waitForFunction(()=>!document.fullscreenElement);
      assert.equal(await page.locator('[data-el="fullscreen"]').innerText(),'Full screen');
      assert.equal(await page.locator('[data-el="fullscreen"]').getAttribute('aria-pressed'),'false');
      assert(await page.locator('canvas').evaluate(el=>document.activeElement===el),'fullscreen exit focuses the game');
      await page.evaluate(()=>document.documentElement.requestFullscreen=()=>Promise.reject(new Error('Fixture denial')));
      await page.locator('[data-el="fullscreen"]').click();
      assert(await page.locator('[data-el="toast"]').isVisible(),'denied fullscreen produces a visible explanation');
      assert.equal(await page.evaluate(()=>document.fullscreenElement),null,'denied fullscreen retains expanded view');
      assert.equal(await page.locator('[data-el="fullscreen"]').getAttribute('aria-pressed'),'false');
      // An optional steps chooser must also fit while all game buttons stay accessible.
      await page.evaluate(()=>{TestLayout.control({t:'steps',on:true}); TestLayout.state('anarchy',1,8640000);});
      await page.waitForFunction(()=>document.querySelector('[data-el="clock"]').textContent.startsWith('RUN 100d '));
      await assertFooterFits(page,`${width}x${height} long run + optional steps`);
      assert(await page.locator('.steps').evaluate(el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;}));
      assert.deepEqual(await page.evaluate(()=>TestLayout.sent.filter(m=>m.t==='vote')),[],'layout checks never cast game votes');
      if(width===1440) {
        const expanded=(await geometry()).canvas;
        await page.goto(new URL('/index.html',base).href);
        await page.waitForSelector('canvas');
        const home=await page.locator('canvas').boundingBox();
        assert(expanded.width>home.width*2 && expanded.height>home.height*2,'desktop expanded screen is substantially larger than homepage');
      }
      await context.close();
      console.log(`PASS play ${width}x${height}: 5 themes, 3:2 screen, 10 visible controls, separate preference/live state, keyboard help, fullscreen entry/exit/denial, no overflow`);
    }
    for (const width of [480,320]) {
      const context=await browser.newContext({viewport:{width,height:480},reducedMotion:'reduce'});
      await context.addInitScript(()=>{window.WebSocket=class {constructor(){this.readyState=1;window.TestLayoutSocket=this;window.TestLayoutSent=[];setTimeout(()=>{this.onopen?.();this.onmessage?.({data:JSON.stringify({t:'state',mode:'democracy',viewers:12,tally:{},windowMs:1500,left:1000,played:0,paused:false,idle:false})});},0);}send(data){TestLayoutSent.push(JSON.parse(data));}close(){}};});
      await context.route('**/*',route=>new URL(route.request().url()).origin===new URL(base).origin?route.continue():route.abort());
      const page=await context.newPage();
      for(const route of ['/embed.html','/index.html']) {
        await page.goto(new URL(route,base).href);
        await page.waitForSelector('canvas');
        await prepareFont(page);
        for(const theme of themes) {
          await page.locator('.xp').evaluate((el,theme)=>el.dataset.theme=theme,theme);
          const g=await page.evaluate(()=> {
            const box=el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
            const buttons=[...document.querySelectorAll('.vbtn')];
            return {overflow:document.documentElement.scrollWidth>innerWidth,canvas:box(document.querySelector('canvas')),controls:buttons.map(el=>({button:el.dataset.btn,round:el.classList.contains('round'),faceWidth:el.offsetWidth,faceHeight:el.offsetHeight,...box(el)})),top:[...document.querySelectorAll('.xp-live,.sound-btn,.seg button')].map(box),footer:[...document.querySelectorAll('.xp-foot a,.keyboard-toggle,.bezel-clock')].map(box),soundInTop:document.querySelector('.xp-top').contains(document.querySelector('.sound-btn')),hingeButtons:document.querySelectorAll('.hinge > [data-btn]').length,clocks:document.querySelectorAll('[data-el="clock"]').length};
          });
          const label=`${route} ${width} ${theme}`;
          const overlaps=(a,b)=>a.x<b.right-.5&&a.right>b.x+.5&&a.y<b.bottom-.5&&a.bottom>b.y+.5;
          assert(!g.overflow,`${label}: no horizontal overflow`);
          assert.equal(new Set(g.controls.map(r=>r.button)).size,10,`${label}: exactly ten unique game buttons`);
          assert.equal(g.controls.length,10,`${label}: no duplicate game buttons`);
          assert.equal(g.hingeButtons,2,`${label}: L/R belong to hinge ends`);
          assert.equal(g.clocks,1,`${label}: single run clock`);
          assert(g.controls.every(r=>r.x>=0&&r.right<=width&&(route!=='/embed.html'||r.bottom<=480)),`${label}: game buttons remain in viewport`);
          assert([...g.controls,...g.top,...g.footer].every(r=>!overlaps(r,g.canvas)),`${label}: game picture clear of controls and footer`);
          for(let i=0;i<g.controls.length;i++) for(let j=i+1;j<g.controls.length;j++) {
            const a=g.controls[i],b=g.controls[j];
            const touching=a.round&&b.round?Math.hypot(a.x+a.width/2-b.x-b.width/2,a.y+a.height/2-b.y-b.height/2)<(a.faceWidth+b.faceWidth)/2-.5:overlaps(a,b);
            assert(!touching,`${label}: buttons do not overlap ${a.button}/${b.button}`);
          }
          assert(g.controls.filter(r=>['up','down','left','right'].includes(r.button)).every(r=>r.faceWidth===32&&r.faceHeight===32),`${label}: full-size Dpad arms`);
          assert(g.controls.filter(r=>r.round).every(r=>r.faceWidth>=42),`${label}: comfortable A/B faces`);
          assert(g.controls.filter(r=>['l','r'].includes(r.button)).every(r=>r.faceWidth>=64&&r.faceHeight>=24),`${label}: larger shoulder targets`);
          assert(g.soundInTop,`${label}: sound in top bar`);
          assert(Math.abs(g.canvas.width/g.canvas.height-1.5)<.01,`${label}: picture stays 3:2`);
          if(route==='/embed.html'&&width===480) {
            assert(g.canvas.width>=400&&g.canvas.height>=266,`${label}: substantially larger than old 315x210 game`);
            assert(g.canvas.width*g.canvas.height>=315*210*1.6,`${label}: at least 60% more picture area`);
          }
        }
        await page.locator('.xp').evaluate(el=>el.dataset.theme='flame');
        for(const [bots,viewers] of [[5,2],[50,123456],[0,12]]) {
          await page.evaluate(([simulatedPlayers,viewers])=>TestLayoutSocket.onmessage({data:JSON.stringify({t:'state',mode:'anarchy',anarchyShare:1,modeVoters:2,viewers,simulatedPlayers,tally:{},windowMs:1500,left:1000,played:0,paused:false,idle:false})}),[bots,viewers]);
          assert.equal(await page.locator('[data-el="watcherCount"]').innerText(),`${viewers+bots} playing`,`${route} ${width}: combined participant count`);
          assert(await page.locator('.seg').evaluate(el=>el.getBoundingClientRect().right<=innerWidth),`${route} ${width}: modes fit beside bot count`);
        }
        await page.locator('[data-el="watcherCount"]').evaluate(el=>el.textContent='123456 playing');
        assert(await page.locator('.seg').evaluate(el=>el.getBoundingClientRect().right<=innerWidth),`${route} ${width}: large viewer counts cannot displace mode controls`);
        if(route==='/embed.html') {
          if(previewFrame) {
            await page.locator('.seg [data-mode="anarchy"]').click();
            await page.evaluate(()=>TestLayoutSocket.onmessage({data:JSON.stringify({t:'state',mode:'anarchy',anarchyShare:1,modeVoters:2,viewers:2,tally:{},windowMs:1500,left:1000,played:0,paused:false,idle:false})}));
            await drawCapturedFrame(page);
            await page.screenshot({path:path.join(shots,`embed-${width}x480.png`)});
            console.log(`PREVIEW embed ${width}x480: canvas ${JSON.stringify(await page.locator('canvas').boundingBox())}`);
          }
          await page.evaluate(()=> {
            TestLayoutSocket.onmessage({data:JSON.stringify({t:'steps',on:true})});
            TestLayoutSocket.onmessage({data:JSON.stringify({t:'state',mode:'anarchy',anarchyShare:1,modeVoters:2,viewers:2,tally:{},windowMs:1500,left:1000,played:8640000,paused:false,idle:false})});
          });
          await page.waitForFunction(()=>document.querySelector('[data-el="clock"]').textContent.startsWith('RUN 100d '));
          await assertFooterFits(page,`embed ${width} long run + optional steps`);
          assert(await page.locator('.xp-foot').evaluate(el=>[...el.querySelectorAll('a,button,.bezel-clock')].filter(el=>el.getBoundingClientRect().width>0).every(el=>{const r=el.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight;})),`${width}: optional steps and footer controls fit`);
          assert(await page.evaluate(()=>document.documentElement.scrollHeight<=innerHeight),`${width}: optional steps keep embed inside viewport`);
          assert.deepEqual(await page.evaluate(()=>TestLayoutSent.filter(m=>m.t==='vote')),[],'compact layout checks never cast game votes');
          await page.locator('.hinge [data-btn="l"]').click();
          await page.waitForTimeout(220);
          await page.locator('.hinge [data-btn="r"]').click();
          assert.deepEqual(await page.evaluate(()=>TestLayoutSent.filter(m=>m.t==='vote')),[{t:'vote',b:'l',n:1},{t:'vote',b:'r',n:1}],`${width}: moved shoulder pointers cast exactly one correct vote each`);
        }
      }
      await context.close();
      console.log(`PASS compact ${width}x480: 5 themes, larger picture, full-size controls, hinge shoulders, optional footer fits`);
    }
    const unsupported=await browser.newContext({viewport:{width:390,height:844}});
    await unsupported.addInitScript(()=> {
      Object.defineProperty(document,'fullscreenEnabled',{value:false});
      window.WebSocket=class {constructor(){this.readyState=1;}send(){}close(){}};
    });
    await unsupported.route('**/*',route=>new URL(route.request().url()).origin===new URL(base).origin?route.continue():route.abort());
    const unsupportedPage=await unsupported.newPage();
    await unsupportedPage.goto(new URL('/play.html',base).href);
    await unsupportedPage.waitForSelector('canvas');
    assert(await unsupportedPage.locator('[data-el="fullscreen"]').isHidden(),'unsupported API hides native fullscreen control');
    assert((await unsupportedPage.locator('canvas').boundingBox()).width>300,'expanded game works without native fullscreen');
    await unsupported.close();
    console.log('PASS unsupported fullscreen: expanded game remains available, native toggle hidden');
    console.log(`Screenshots: ${shots} (${previewFrame?'local layout fixture with captured public frame':'synthetic local layout fixture'})`);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
