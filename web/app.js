/* PlayX: live client
 * Draws the stream from the run server, plays its audio, and sends votes.
 * Wire format (binary): [kind u8: 1 key, 2 delta][audioLen u16][seq u32][mu-law audio][zlib RGB565 frame or XOR delta]
 */
(() => {
  "use strict";

  const MODE = document.body.dataset.mode || "page";
  // The small link on the handheld; the server fills these in from XP_FOOTER_TEXT/XP_FOOTER_URL.
  const pageSetting = (v) => (v && !v.startsWith("__") ? v : "");
  const FOOTER_TEXT = pageSetting(document.body.dataset.footerText) || "PlayX";
  const FOOTER_URL = /^https?:\/\//i.test(pageSetting(document.body.dataset.footerUrl))
    ? document.body.dataset.footerUrl : "https://github.com/Stormster/playx";
  const root = document.getElementById("app");
  const W = 240, H = 160;

  const BUTTONS = ["up", "down", "left", "right", "a", "b", "l", "r", "start", "select"];
  const DIRS = new Set(["up", "down", "left", "right"]);
  const LABEL = { up: "▲", down: "▼", left: "◀", right: "▶", a: "A", b: "B", l: "L", r: "R", start: "START", select: "SELECT" };
  const TICK = { up: "Up", down: "Down", left: "Left", right: "Right", a: "A", b: "B", l: "L", r: "R", start: "Start", select: "Select" };
  const STEP_CHOICES = [1, 2, 3, 5];

  const THEMES = [
    { id: "flame", name: "Flame Red" },
    { id: "cobalt", name: "Cobalt" },
    { id: "graphite", name: "Graphite" },
    { id: "pearl", name: "Pearl" },
    { id: "lime", name: "PlayX" },
  ];

  // ---------------------------------------------------------------------------
  // Identity (anonymous, per browser)
  // ---------------------------------------------------------------------------
  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  }

  // "example.dev" -> "example<span>.dev</span>" (the suffix takes the accent colour)
  function footerMark() {
    const dot = FOOTER_TEXT.lastIndexOf(".");
    if (dot <= 0) return escapeHtml(FOOTER_TEXT);
    return `${escapeHtml(FOOTER_TEXT.slice(0, dot))}<span>${escapeHtml(FOOTER_TEXT.slice(dot))}</span>`;
  }

  function voterId() {
    let id = null;
    try { id = localStorage.getItem("xp-id"); } catch {}
    if (!id) {
      id = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
      try { localStorage.setItem("xp-id", id); } catch {}
    }
    return id;
  }
  const ME = voterId();
  function savedModePref() {
    try { const m = localStorage.getItem("xp-mode"); return m === "anarchy" || m === "democracy" ? m : null; } catch { return null; }
  }

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  const st = {
    mode: "democracy", myModePref: savedModePref(), anarchyShare: 0, modeVoters: 0,
    modeLockEndsAt: 0, modeFlipAt: 0,
    tally: {}, mine: null, serverMine: null, windowMs: 1500, windowEndsAt: 0,
    viewers: 0, simulatedPlayers: 0, played: 0, playedAt: performance.now(), paused: false,
    steps: 1, stepsOn: false, connected: false, anarchyHoldUnlimited: false,
  };
  const els = {};

  // ---------------------------------------------------------------------------
  // Video
  // ---------------------------------------------------------------------------
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  canvas.setAttribute("aria-label", "Live game screen");
  canvas.tabIndex = 0;
  canvas.setAttribute("aria-describedby", "keyboard-tip");
  canvas.addEventListener("pointerdown", () => canvas.focus({ preventScroll: true }));
  const ctx = canvas.getContext("2d");
  const image = ctx.createImageData(W, H);
  const pixels32 = new Uint32Array(image.data.buffer);
  const frame565 = new Uint16Array(W * H);
  const LUT = new Uint32Array(65536);
  for (let c = 0; c < 65536; c++) {
    const r = ((c >> 11) & 31) * 255 / 31, g = ((c >> 5) & 63) * 255 / 63, b = (c & 31) * 255 / 31;
    LUT[c] = (255 << 24) | (Math.round(b) << 16) | (Math.round(g) << 8) | Math.round(r);   // little-endian RGBA
  }
  let haveKey = false;

  async function inflate(bytes) {
    const ds = new DecompressionStream("deflate");
    const out = new Response(new Blob([bytes]).stream().pipeThrough(ds));
    return new Uint16Array(await out.arrayBuffer());
  }

  let chain = Promise.resolve();
  function resetKeyAfterFrames() {
    const generation = src.generation;
    chain = chain.then(() => { if (src.generation === generation) haveKey = false; });
  }
  function onPacket(buf, silent) {
    const generation = src.generation;
    const dv = new DataView(buf);
    const kind = dv.getUint8(0), alen = dv.getUint16(1, true);
    const audio = new Uint8Array(buf, 7, alen);
    const video = new Uint8Array(buf, 7 + alen);
    if (!silent) playAudio(audio);
    chain = chain.then(async () => {
      if (src.generation !== generation) return;
      if (kind === 2 && !haveKey) return;
      const px = await inflate(video);
      if (src.generation !== generation) return;
      if (kind === 1) { frame565.set(px); haveKey = true; }
      else for (let i = 0; i < px.length; i++) frame565[i] ^= px[i];
      for (let i = 0; i < frame565.length; i++) pixels32[i] = LUT[frame565[i]];
      ctx.putImageData(image, 0, 0);
    }).catch((e) => console.warn("frame", e));
  }

  // ---------------------------------------------------------------------------
  // CDN chunk player: chunks are ~2 s, each starts with a keyframe. We fetch them
  // as the server announces them and play packets at the stream's frame rate.
  // ---------------------------------------------------------------------------
  const FRAME_MS = 1000 / 29.86;
  const UPLOAD_MS = 150;            // rough upload + announce time, for the delay estimate
  // How far the picture runs behind the server. Vote UI (ring, tally, wins) is shown
  // this much later so it lines up with what the viewer actually sees.
  const sync = { delay: 0 };
  const later = (fn) => (sync.delay < 30 ? fn() : setTimeout(fn, sync.delay));
  // Startup cushion: how long to buffer before playing, grown a step each time playback runs dry.
  // Chunks need a big one; the live relay delivers every ~100 ms so it needs very little.
  const CHUNK_CUSHION = { base: 600, cap: 2000, step: 250 };
  const RELAY_CUSHION = { base: 150, cap: 600, step: 60 };
  let cushion = CHUNK_CUSHION;
  const player = { queue: [], lastN: -1, fetchChain: Promise.resolve(), playing: false,
                   nextAt: 0, startAt: 0, startDelay: CHUNK_CUSHION.base, chunks: 0, atEnd: false,
                   smooth: 0 };
  const src = { name: "chunks", generation: 0 }; // reject work from an earlier path or server connection

  function startPlayback() {
    if (player.playing) return;
    player.playing = true;
    player.startAt = performance.now() + player.startDelay;
    player.nextAt = player.startAt;
  }

  // end = the server froze the game after this chunk (idle, empty, host pause), so running
  // out of video afterwards is a pause, not lag, and shouldn't grow the startup cushion.
  function onChunkAnnounce(n, url, end) {
    if (src.name !== "chunks") return;
    const generation = src.generation;
    if (n === player.lastN && end) {
      // the pause landed exactly on a chunk boundary: the server re-marks the newest chunk as the end
      player.fetchChain = player.fetchChain.then(() => {
        if (src.name !== "chunks" || src.generation !== generation) return;
        const last = player.queue[player.queue.length - 1];
        if (last) last.end = true; else player.atEnd = true;
      });
      return;
    }
    if (n <= player.lastN) return;
    if (player.lastN < 0 || n > player.lastN + 3) player.queue = [];   // first chunk or far behind: start fresh
    player.lastN = n;
    const announcedAt = performance.now();
    player.fetchChain = player.fetchChain.then(async () => {
      if (src.name !== "chunks" || src.generation !== generation) return;
      try {
        const r = await fetch(url, { cache: "force-cache" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const buf = await r.arrayBuffer();
        if (src.name !== "chunks" || src.generation !== generation) return;
        enqueueChunk(buf, announcedAt, !!end);
      } catch (e) { console.warn("chunk", n, e); }
    });
  }

  function enqueueChunk(buf, announcedAt, end) {
    const dv = new DataView(buf);
    if (String.fromCharCode(...new Uint8Array(buf, 0, 4)) !== "XPC1") return;
    const count = dv.getUint16(8, true);
    let off = 10;
    for (let i = 0; i < count; i++) {
      const len = dv.getUint32(off, true); off += 4;
      player.queue.push({ buf: buf.slice(off, off + len), chunkStart: i === 0,
                          made: i === 0 ? announcedAt - count * FRAME_MS - UPLOAD_MS : 0,
                          end: end && i === count - 1 });
      off += len;
    }
    player.chunks++;
    // more than ~6 s buffered: drop whole chunks from the front to stay close to live
    while (player.queue.length > 180) {
      player.queue.shift();
      while (player.queue.length && !player.queue[0].chunkStart) player.queue.shift();
    }
    startPlayback();
  }

  // Relay delay estimate: the state messages (sent straight from the server every 250 ms)
  // carry the newest packet number, so "when did the server have packet N" is known here.
  const stateMark = { seq: -1, at: 0 };

  function playTick(now) {
    if (!player.playing || now < player.startAt) return;
    if (now - player.nextAt > 400) player.nextAt = now;      // tab was asleep; don't fast-forward
    if (src.name === "relay") {
      // fell behind (slow device, hidden tab): skip ahead silently to stay near live
      const target = Math.ceil(player.startDelay / FRAME_MS) + 3;
      while (player.queue.length > target + 9) onPacket(player.queue.shift().buf, true);
    }
    while (player.queue.length && now >= player.nextAt) {
      const p = player.queue.shift();
      if (p.made) {
        const sample = Math.max(0, Math.min(10000, now - p.made));
        sync.delay = sync.delay ? sync.delay * 0.8 + sample * 0.2 : sample;
      } else if (p.relay && stateMark.at && ++player.smooth % 10 === 0) {
        const seq = new DataView(p.buf).getUint32(3, true);
        const sample = Math.max(0, Math.min(10000, now - (stateMark.at + (seq - stateMark.seq) * FRAME_MS)));
        sync.delay = sync.delay ? sync.delay * 0.8 + sample * 0.2 : sample;
        // smooth for ~10 s: ease the cushion back down
        if (player.smooth >= 300) {
          player.smooth = 0;
          player.startDelay = Math.max(cushion.base, player.startDelay - 30);
        }
      }
      onPacket(p.buf);
      player.atEnd = !!p.end;
      player.nextAt += FRAME_MS;
    }
    if (!player.queue.length && now >= player.nextAt) {
      // ran dry: wait for more with a slightly bigger cushion (unless it was a pause)
      player.playing = false;
      if (!player.atEnd) {
        player.startDelay = Math.min(cushion.cap, player.startDelay + cushion.step);
        player.smooth = 0;
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Live relay (playx-live): video pushed straight to us, ~0.3 to 0.6 s behind the game.
  // If it can't be reached or stalls, we tell the server and play R2 chunks instead.
  // ---------------------------------------------------------------------------
  const relay = { url: null, serverUp: false, ws: null, working: false, lastMsg: 0,
                  ended: false, lastSeq: -1, endSeq: -1, retry: 0, reported: false, timer: 0, rawPaused: false };

  function useSource(name, reset = false) {
    if (src.name === name && !reset) return;
    src.name = name;
    src.generation++;
    cushion = name === "relay" ? RELAY_CUSHION : CHUNK_CUSHION;
    player.queue = []; player.playing = false; player.lastN = -1; player.atEnd = false;
    player.startDelay = cushion.base; player.smooth = 0;
    player.fetchChain = Promise.resolve();
    chain = Promise.resolve();
    haveKey = false;   // the other path or server connection isn't continuous with this one
  }

  function resetVideoConnection() {
    // Chunk numbers restart on server boot. Every connection starts a new stream,
    // including when the current source already is chunks.
    const oldRelay = relay.ws;
    relay.ws = null;
    clearTimeout(relay.timer);
    if (oldRelay) oldRelay.close();
    relay.url = null; relay.serverUp = false; relay.working = false;
    relay.ended = false; relay.lastSeq = -1; relay.endSeq = -1;
    relay.retry = 0; relay.reported = false; relay.rawPaused = false;
    stateMark.seq = -1; stateMark.at = 0; sync.delay = 0;
    useSource("chunks", true);
  }

  function unpackBatch(data) {
    const dv = new DataView(data), out = [];
    let off = 1;
    while (off + 4 <= data.byteLength) {
      const n = dv.getUint32(off, true); off += 4;
      out.push(data.slice(off, off + n)); off += n;
    }
    return out;
  }

  function connectRelay() {
    if (!relay.url || relay.ws) return;
    clearTimeout(relay.timer);
    let ws;
    try { ws = new WebSocket(relay.url); } catch { return relayFailed(); }
    ws.binaryType = "arraybuffer";
    relay.ws = ws;
    relay.lastMsg = performance.now();
    ws.onmessage = (e) => { if (relay.ws === ws) onRelayMessage(e.data); };
    ws.onclose = () => {
      if (relay.ws !== ws) return;
      relay.ws = null;
      relayFailed();
    };
  }

  function relayFailed() {
    relay.working = false;
    if (src.name === "relay") useSource("chunks");
    if (relay.serverUp && !relay.reported) { send({ t: "fallback" }); relay.reported = true; }
    clearTimeout(relay.timer);
    relay.timer = setTimeout(connectRelay, Math.min(60000, 2000 * 2 ** relay.retry++));
  }

  function onRelayMessage(data) {
    if (!(data instanceof ArrayBuffer) || !data.byteLength) return;
    relay.lastMsg = performance.now();
    if (!relay.serverUp) return;                 // server says the relay is down: stay on chunks
    if (!relay.working) {
      relay.working = true; relay.retry = 0;
      useSource("relay");
      if (relay.reported) { send({ t: "relayok" }); relay.reported = false; }
    }
    const t = new Uint8Array(data, 0, 1)[0];
    if (t === 3) {                               // game froze: a gap now is a pause
      relay.ended = true;
      relay.endSeq = relay.lastSeq;
      const last = player.queue[player.queue.length - 1];
      if (last) last.end = true; else player.atEnd = true;
      return;
    }
    const pkts = unpackBatch(data);
    if (pkts.length) relay.lastSeq = new DataView(pkts[pkts.length - 1]).getUint32(3, true);
    if (t === 1) {
      relay.ended = false;
      for (const p of pkts) player.queue.push({ buf: p, relay: true });
      if (player.queue.length > 300) {           // way behind (tab hidden a long time): resync
        player.queue = [];
        resetKeyAfterFrames();
      }
      startPlayback();
    } else if (t === 2 || t === 4) {
      // 2: catch-up to live (keyframe + what followed). 4: still picture of a frozen game.
      player.queue = []; player.playing = false;
      for (const p of pkts) onPacket(p, true);
      if (t === 4) {
        resetKeyAfterFrames();   // live resumes from a fresh keyframe
        relay.ended = true; player.atEnd = true;
        relay.endSeq = relay.lastSeq;
      } else relay.ended = false;
    }
  }

  function onVideoStatus(up) {
    if (up && !relay.serverUp) {
      relay.lastMsg = performance.now();
      relay.ended = false;
    }
    relay.serverUp = up;
    if (!up) {
      // server lost the relay and turned chunks on for everyone
      relay.working = false;
      useSource("chunks");
    } else connectRelay();
  }

  // read-only peek for debugging in the console: PlayXVideo()
  window.PlayXVideo = () => ({ source: src.name, relayConnected: !!relay.ws, relayWorking: relay.working,
                               onFallback: relay.reported, delayMs: Math.round(sync.delay),
                               cushionMs: player.startDelay, queued: player.queue.length });

  // stall watchdog: the server is live but the relay has gone quiet
  setInterval(() => {
    if (!relay.url || !relay.ws || !relay.serverUp) return;
    if (relay.working && (relay.ended || relay.rawPaused)) return;
    if (performance.now() - relay.lastMsg > 3000) {
      const ws = relay.ws;
      relay.ws = null;
      try { ws && ws.close(); } catch {}
      relayFailed();
    }
  }, 1000);

  // ---------------------------------------------------------------------------
  // Audio (mu-law mono, scheduled with a small jitter buffer)
  // ---------------------------------------------------------------------------
  const audio = { ctx: null, muted: true, rate: 16384, next: 0 };
  const ULAW = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const u = ~i & 0xff, sign = u & 0x80, exp = (u >> 4) & 7, mant = u & 15;
    let s = ((mant << 3) + 0x84) << exp; s -= 0x84;
    ULAW[i] = (sign ? -s : s) / 32768;
  }
  function unlockAudio() {
    try {
      if (!audio.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        audio.ctx = new AC();
      }
      if (audio.ctx.state === "suspended") audio.ctx.resume();
    } catch {}
  }
  function playAudio(bytes) {
    if (audio.muted || !audio.ctx || audio.ctx.state !== "running" || !bytes.length) return;
    const buf = audio.ctx.createBuffer(1, bytes.length, audio.rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < bytes.length; i++) ch[i] = ULAW[bytes[i]] * 0.8;
    const now = audio.ctx.currentTime;
    if (audio.next < now + 0.03 || audio.next > now + 0.5) audio.next = now + 0.12;
    const src = audio.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(audio.ctx.destination);
    src.start(audio.next);
    audio.next += buf.duration;
  }

  // ---------------------------------------------------------------------------
  // Socket
  // ---------------------------------------------------------------------------
  let ws = null, retry = 0;
  function connect() {
    const socket = new WebSocket(location.origin.replace(/^http/, "ws") + "/ws");
    ws = socket;
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      if (ws !== socket) return;
      resetVideoConnection();
      retry = 0; st.connected = true;
      send({ t: "hello", id: ME });
      if (st.myModePref) send({ t: "mode", m: st.myModePref });
      paintStatus();
    };
    socket.onmessage = (e) => {
      if (ws !== socket) return;
      if (typeof e.data !== "string") return onPacket(e.data);
      const m = JSON.parse(e.data);
      if (m.t === "hello") {
        setAnarchyHoldUnlimited(!!m.anarchyHoldUnlimited);
        if (Number.isInteger(m.viewers) && m.viewers >= 0) st.viewers = m.viewers;
        st.simulatedPlayers = validSimulatedCount(m.simulatedPlayers);
        paintStatus();
        audio.rate = m.audioRate; if (m.theme) applyTheme(m.theme);
        setStepsOn(!!m.steps);
        relay.url = m.relay || null;
        if (relay.reported) send({ t: "fallback" });     // new server connection: still on chunks
        if (relay.url) onVideoStatus(!!m.relayUp);
      }
      else if (m.t === "video") onVideoStatus(!!m.relayUp);
      else if (m.t === "steps") setStepsOn(m.on);
      else if (m.t === "holdsetting") setAnarchyHoldUnlimited(!!m.anarchyHoldUnlimited);
      else if (m.t === "chunk") onChunkAnnounce(m.n, m.url, m.end);
      else if (m.t === "state") {
        if (m.seq !== stateMark.seq) { stateMark.seq = m.seq; stateMark.at = performance.now(); }
        if (relay.ended && m.seq > relay.endSeq) relay.ended = false;
        relay.rawPaused = m.playing == null ? m.paused : !m.playing;
        onState(m);
      }
      else if (m.t === "win") later(() => onWin(m));
      else if (m.t === "modechange") onModeChange(m);
      else if (m.t === "modelock") {
        st.modeLockEndsAt = m.lockMs > 0 ? performance.now() + m.lockMs : 0;
        st.modeFlipAt = 0;
        paintModeLabel(performance.now());
      }
      else if (m.t === "notice") toast(m.text);
      else if (m.t === "inputlimit") {
        serverCooldownUntil = performance.now() + m.pauseMs;
        stopKeyboardHold();
        toast("Take a short pause, then press again.");
      }
      else if (m.t === "theme") applyTheme(m.id);
    };
    socket.onclose = () => {
      if (ws !== socket) return;
      stopKeyboardHold();
      ws = null;
      st.connected = false; paintStatus();
      setTimeout(connect, Math.min(10000, 500 * 2 ** retry++));
    };
  }
  // ×2/×3/×5 picker: the host can turn it off, and then every direction vote is one step
  function setStepsOn(on) {
    st.stepsOn = on;
    const pick = root.querySelector(".steps");
    if (pick) pick.hidden = !on;
    document.querySelectorAll("[data-steps-help]").forEach((el) => (el.hidden = !on));
    if (!on) root.querySelector('.steps button[data-steps="1"]')?.click();
  }
  const send = (o) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); };

  function onState(m) {
    setAnarchyHoldUnlimited(!!m.anarchyHoldUnlimited);
    if (st.mode !== m.mode) stopKeyboardHold();
    st.mode = m.mode; st.anarchyShare = m.anarchyShare; st.viewers = m.viewers;
    st.simulatedPlayers = validSimulatedCount(m.simulatedPlayers);
    st.modeVoters = m.modeVoters || 0;
    st.modeLockEndsAt = m.modeLockLeft > 0 ? performance.now() + m.modeLockLeft : 0;
    st.modeFlipAt = m.modeFlipIn == null ? 0 : performance.now() + m.modeFlipIn;
    st.tally = m.tally || {}; st.windowMs = m.windowMs;
    // Keep adding my fresh vote until a state that already counts it arrives, or the
    // window it was cast in has closed (the closing state never reports it as mine).
    const lm0 = st.localMine, endsAt = performance.now() + m.left;
    if (lm0 && (m.mine === lm0.key || endsAt > lm0.windowEndsAt + m.windowMs / 2)) st.localMine = null;
    st.serverMine = m.mine || null;
    const lm = freshVote();
    st.mine = lm ? lm.key : st.serverMine;
    st.windowEndsAt = endsAt;
    st.played = m.played; st.playedAt = performance.now(); st.paused = m.paused; st.idle = m.idle;
    paintStatus(); paintTally(); paintModeLabel(performance.now());
  }

  const ARROW = { up: "↑", down: "↓", left: "←", right: "→" };
  function chipLabel(b, n) {
    return ARROW[b] ? ARROW[b].repeat(Math.max(1, n)) : TICK[b];
  }
  function onWin(m) {
    const key = `${m.b}:${m.n}`;
    const first = els.ticker.firstElementChild;
    const now = performance.now();
    el_flash(m.b);
    // same input again (anarchy spam, or a crowd that keeps voting A): bump one chip instead of adding more.
    // No count on it: a press can turn, bump a wall or still be queued, so "×7" read as 7 tiles moved.
    if (first && first.dataset.key === key && now - Number(first.dataset.at) < 4000) {
      first.dataset.at = now;
      if (m.you) first.classList.add("me");
      first.classList.remove("bump"); void first.offsetWidth; first.classList.add("bump");
      return;
    }
    const chip = document.createElement("span");
    chip.className = "chip" + (m.you ? " me" : "");
    chip.dataset.key = key; chip.dataset.at = now;
    chip.textContent = chipLabel(m.b, m.n);
    chip.title = m.you ? "Your vote won" : "";
    els.ticker.prepend(chip);
    while (els.ticker.children.length > 6) els.ticker.lastChild.remove();
  }
  function el_flash(b) {
    const el = els.buttons[b];
    if (el) { el.classList.remove("won"); void el.offsetWidth; el.classList.add("won"); }
  }

  function castVote(btn) {
    const n = DIRS.has(btn) ? st.steps : 1;
    send({ t: "vote", b: btn, n });
    if (st.mode === "democracy") {
      st.mine = `${btn}:${n}`;
      st.localMine = st.mine === st.serverMine ? null
        : { key: st.mine, until: performance.now() + st.windowMs + 500, windowEndsAt: st.windowEndsAt };
      paintTally();
    }
  }

  // ---------------------------------------------------------------------------
  // UI
  // ---------------------------------------------------------------------------
  function vbtn(btn, extra = "", label = LABEL[btn]) {
    return `<button class="vbtn ${btn} ${extra}" data-btn="${btn}" aria-label="Vote ${TICK[btn]}">
      <span class="face"><span class="bar"></span><span class="lbl">${label}</span></span><span class="cnt"></span></button>`;
  }

  function buildUI() {
    const grille = Array.from({ length: 8 }, () => "<i></i>").join("");
    root.innerHTML = `
      <div class="xp" data-el="shell" data-theme="flame">
        <div class="lid">
          <div class="xp-top">
            <span class="xp-live" data-el="watchers"><span class="watcher-count" data-el="watcherCount">connecting</span></span>
            <button class="icon-btn sound-btn" data-el="sound" aria-label="Sound off" aria-pressed="false" title="Tap for sound">
              <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1 6h3l4-4v12l-4-4H1z"/><path class="sound-wave" d="M11 5v6m3-8v10"/><path class="sound-mute" d="M10 5l5 6m0-6l-5 6"/></svg>
              <span data-el="soundLabel">TAP FOR SOUND</span>
            </button>
            <span class="xp-spacer"></span>
            <span class="seg" role="group" aria-label="Your vote for the voting mode">
              <button data-mode="democracy" aria-pressed="false">Democracy<span class="pct">--</span></button>
              <button data-mode="anarchy" aria-pressed="false">Anarchy<span class="pct">--</span></button>
            </span>
          </div>
          <div class="bezel">
            <div class="xp-screen">
              <div class="glass" data-el="screen">
                <div class="hud-toast" data-el="toast" role="status" hidden></div>
                <div class="mode-announcement" data-el="modeAnnouncement" role="status" hidden><strong></strong><span></span></div>
              </div>
            </div>
          </div>
        </div>
        <div class="hinge">
          ${vbtn("l", "shoulder")}
          <div class="hinge-bar">
            <span class="hinge-mode" data-el="hudMode">DEMOCRACY</span>
            <div class="hinge-ticker" data-el="ticker" aria-label="Recent winning inputs"></div>
            <div class="hinge-ring" data-el="ring" title="Time left in this vote"><span></span></div>
          </div>
          ${vbtn("r", "shoulder")}
        </div>
        <div class="base">
          <div class="controls">
            <div class="well dpad-well">
              <div class="dpad">${vbtn("up")}${vbtn("left")}<div class="hub"></div>${vbtn("right")}${vbtn("down")}</div>
            </div>
            <div class="center">
              <span class="led" aria-hidden="true"></span>
              <div class="grille" aria-hidden="true">${grille}</div>
              <div class="ss">
                <div class="ss-item"><span class="ss-label">SELECT</span><span class="ss-well">${vbtn("select", "pill", "")}</span></div>
                <div class="ss-item"><span class="ss-label">START</span><span class="ss-well">${vbtn("start", "pill", "")}</span></div>
              </div>
            </div>
            <div class="well ab-well">
              <div class="ab">${vbtn("b", "round")}${vbtn("a", "round")}</div>
            </div>
          </div>
        </div>
        <div class="xp-foot">
          <span class="steps" role="group" aria-label="Steps per direction vote" hidden>
            ${STEP_CHOICES.map((n) => `<button data-steps="${n}" aria-pressed="${n === 1}">×${n}</button>`).join("")}
          </span>
          <a class="bezel-mark" href="${escapeHtml(FOOTER_URL)}" target="_blank" rel="noopener noreferrer">${footerMark()}</a>
          <div class="bezel-clock" data-el="clock" title="Total play time for this run">RUN 00:00:00</div>
          <button class="keyboard-toggle" data-el="keyboardHelp" aria-expanded="false" aria-controls="keyboard-guide">Keyboard</button>
          ${MODE === "play"
            ? '<a href="/">Home</a><button class="fullscreen-toggle" data-el="fullscreen" aria-pressed="false" hidden>Full screen</button>'
            : `<a href="/play" ${MODE === "embed" ? 'target="_blank" rel="noopener"' : ''}>Full screen</a>`}
        </div>
        <section class="keyboard-guide" id="keyboard-guide" aria-label="Keyboard controls" data-el="keyboardGuide" hidden>
          <p id="keyboard-tip">Click or Tab to the game screen to play. Hold a key to repeat.</p>
          <p>Move: arrows or WASD<br>A: Z / J · B: X / K<br>Start: Enter · Select: tap Shift<br>L / R: Q / E</p>
          <p data-hold-help>Anarchy repeats moves for up to 3 seconds, then pauses for 2 seconds; release and press again. Democracy renews your single vote until release. Shift votes on release.</p>
          <p data-steps-help hidden>Steps: 1, 2, 3 or 5</p>
          <button data-el="keyboardClose">Close</button>
        </section>
      </div>`;
    root.querySelectorAll("[data-el]").forEach((el) => (els[el.dataset.el] = el));
    els.screen.prepend(canvas);
    els.buttons = Object.fromEntries([...root.querySelectorAll(".vbtn")].map((b) => [b.dataset.btn, b]));

    root.querySelectorAll(".vbtn").forEach((b) => {
      b.addEventListener("pointerdown", (e) => { e.preventDefault(); castVote(b.dataset.btn); });
      // Pointer votes happen immediately; native keyboard/assistive clicks have no pointer detail.
      b.addEventListener("click", (e) => { if (e.detail === 0) castVote(b.dataset.btn); });
      b.addEventListener("keydown", (e) => {
        if (e.repeat && (e.key === "Enter" || e.key === " ")) e.preventDefault();
      });
    });
    const showKeyboard = (show) => {
      els.keyboardGuide.hidden = !show;
      els.keyboardHelp.setAttribute("aria-expanded", String(show));
    };
    els.keyboardHelp.addEventListener("click", () => showKeyboard(els.keyboardGuide.hidden));
    els.keyboardClose.addEventListener("click", () => { showKeyboard(false); els.keyboardHelp.focus(); });
    if (els.fullscreen && document.fullscreenEnabled && document.documentElement.requestFullscreen) {
      els.fullscreen.hidden = false;
      const paintFullscreen = () => {
        const active = !!document.fullscreenElement;
        els.fullscreen.textContent = active ? "Exit full screen" : "Full screen";
        els.fullscreen.setAttribute("aria-pressed", String(active));
      };
      document.addEventListener("fullscreenchange", () => { stopKeyboardHold(); paintFullscreen(); });
      els.fullscreen.addEventListener("click", async () => {
        try {
          if (document.fullscreenElement) await document.exitFullscreen();
          else await document.documentElement.requestFullscreen();
          canvas.focus({ preventScroll: true });
        } catch {
          toast("Full screen is unavailable here. You can keep playing in this view.");
        }
        paintFullscreen();
      });
    }
    root.querySelectorAll(".seg button").forEach((b) =>
      b.addEventListener("click", () => {
        st.myModePref = b.dataset.mode;
        try { localStorage.setItem("xp-mode", st.myModePref); } catch {}
        send({ t: "mode", m: st.myModePref });
        paintStatus();
      })
    );
    root.querySelectorAll(".steps button").forEach((b) =>
      b.addEventListener("click", () => {
        st.steps = Number(b.dataset.steps);
        root.querySelectorAll(".steps button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      })
    );
    const toggleSound = () => {
      unlockAudio();
      audio.muted = !audio.muted;
      els.soundLabel.textContent = audio.muted ? "SOUND OFF" : "SOUND ON";
      els.sound.setAttribute("aria-pressed", String(!audio.muted));
      try { localStorage.setItem("xp-audio-used", "1"); } catch {}
      els.sound.setAttribute("aria-label", audio.muted ? "Sound off" : "Sound on");
      els.sound.title = audio.muted ? "Turn sound on" : "Turn sound off";
      els.sound.classList.remove("nudge");
    };
    els.sound.addEventListener("click", toggleSound);
    let audioUsed = false;
    try { audioUsed = localStorage.getItem("xp-audio-used") === "1"; } catch {}
    els.sound.classList.toggle("nudge", !audioUsed);
    if (audioUsed) els.soundLabel.textContent = "SOUND OFF";
    applyTheme("flame");
  }

  function applyTheme(id) {
    if (!THEMES.some((t) => t.id === id) || !els.shell) return;
    els.shell.dataset.theme = id;

  }

  function validSimulatedCount(count) {
    return Number.isInteger(count) && count >= 0 && count <= 50 ? count : 0;
  }

  function paintStatus() {
    const bots = st.connected ? st.simulatedPlayers : 0;
    els.watcherCount.textContent = st.connected ? `${st.viewers + bots} playing` : "reconnecting";
    const audience = st.connected ? els.watcherCount.textContent : "Reconnecting";
    els.watchers.title = audience;
    els.watchers.setAttribute("aria-label", audience);
    els.ring.style.visibility = st.mode === "democracy" ? "visible" : "hidden";
    root.querySelectorAll(".seg button").forEach((b) => {
      const pressed = String(b.dataset.mode === st.myModePref);
      if (b.getAttribute("aria-pressed") !== pressed) b.setAttribute("aria-pressed", pressed);
      b.classList.toggle("live", b.dataset.mode === st.mode);
      const share = b.dataset.mode === "anarchy" ? st.anarchyShare : 1 - st.anarchyShare;
      const pct = b.querySelector(".pct");
      const text = st.modeVoters ? `${Math.round(share * 100)}%` : "--";
      if (pct.textContent !== text) pct.textContent = text;
    });
    els.shell.style.setProperty("--anarchy", st.anarchyShare);
  }

  function freshVote() {
    const lm = st.localMine;
    return lm && performance.now() < lm.until ? lm : null;
  }

  function paintTally() {
    const mineBtn = st.mine ? st.mine.split(":")[0] : null;
    // My fresh vote shows instantly on top of the crowd's counts, moving off my
    // previous pick, until the server's tally includes it.
    const counts = {};
    for (const b of BUTTONS) counts[b] = st.tally[b] || 0;
    const lm = freshVote();
    if (lm) {
      const b = lm.key.split(":")[0], was = st.serverMine && st.serverMine.split(":")[0];
      if (was && was !== b) counts[was] = Math.max(0, counts[was] - 1);
      if (was !== b) counts[b] += 1;
    }
    const total = BUTTONS.reduce((s, b) => s + counts[b], 0);
    for (const b of BUTTONS) {
      const el = els.buttons[b], n = counts[b];
      el.style.setProperty("--share", total ? (n / total).toFixed(3) : 0);
      el.querySelector(".cnt").textContent = st.mode === "democracy" && n ? n : "";
      el.classList.toggle("mine", st.mode === "democracy" && mineBtn === b);
    }
  }

  function fmtClock(sec) {
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400), h = Math.floor(sec / 3600) % 24, m = Math.floor(sec / 60) % 60, s = sec % 60;
    const hms = [h, m, s].map((x) => String(x).padStart(2, "0")).join(":");
    return d ? `${d}d ${hms}` : hms;
  }

  let toastTimer = null;
  function toast(text) {
    els.toast.textContent = text;
    els.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (els.toast.hidden = true), 5000);
  }

  let announcementTimer = null;
  let announcedMode = null;
  function onModeChange(m) {
    if (m.mode !== "anarchy" && m.mode !== "democracy") return;
    const changed = st.mode !== m.mode;
    if (changed) { stopKeyboardHold(); st.localMine = null; }
    st.mode = m.mode;
    st.modeLockEndsAt = m.lockMs > 0 ? performance.now() + m.lockMs : 0;
    st.modeFlipAt = 0;
    paintStatus(); paintTally(); paintModeLabel(performance.now());
    // State broadcasts follow this event; they must not restart the celebration.
    if (!changed && announcedMode === m.mode) return;
    announcedMode = m.mode;
    els.modeAnnouncement.querySelector("strong").textContent = m.mode === "anarchy" ? "ANARCHY TAKES OVER" : "DEMOCRACY WINS";
    els.modeAnnouncement.querySelector("span").textContent = m.mode === "anarchy" ? "EVERY PRESS COUNTS" : "THE CROWD CHOOSES";
    els.modeAnnouncement.hidden = false;
    clearTimeout(announcementTimer);
    announcementTimer = setTimeout(() => (els.modeAnnouncement.hidden = true), 2400);
  }

  function paintModeLabel(now) {
    const lockLeft = st.modeLockEndsAt - now;
    const other = st.mode === "democracy" ? "ANARCHY" : "DEMOCRACY";
    let text = st.mode.toUpperCase();
    if (st.modeFlipAt && st.modeFlipAt > now) text += ` > ${other} ${Math.ceil((st.modeFlipAt - now) / 1000)}s`;
    else if (st.modeLockEndsAt && lockLeft >= 1000) text += ` ${fmtClock(lockLeft / 1000).replace(/^00:/, "")}`;
    if (els.hudMode.textContent !== text) els.hudMode.textContent = text;
    els.hudMode.title = lockLeft > 0 ? "Mode is locked until the timer runs out" : "Vote for a mode with the toggle above";
  }

  function tick(now) {
    playTick(now);
    paintModeLabel(now);
    if (st.mode === "democracy") {
      const left = Math.max(0, st.windowEndsAt - now);
      els.ring.style.setProperty("--p", (1 - left / st.windowMs).toFixed(3));
    }
    // A state message may arrive after this animation frame's timestamp was sampled.
    const played = st.played + (st.paused || !st.connected ? 0 : Math.max(0, (now - st.playedAt) / 1000));
    els.clock.textContent = `RUN ${fmtClock(played)}`;
    requestAnimationFrame(tick);
  }

  // ---------------------------------------------------------------------------
  // Keyboard
  // ---------------------------------------------------------------------------
  const KEYMAP = {
    ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    w: "up", s: "down", a: "left", d: "right",
    z: "a", j: "a", x: "b", k: "b", Enter: "start", Shift: "select", q: "l", e: "r",
  };
  let shiftTarget = null;
  const HOLD_MS = 300, HOLD_LIMIT_MS = 3000, HOLD_COOLDOWN_MS = 2000;
  const heldKeys = new Map();
  let keyboardHold = null, holdTimer = null, burstCooldownUntil = 0, serverCooldownUntil = 0;
  function setAnarchyHoldUnlimited(on) {
    if (st.anarchyHoldUnlimited === on) return;
    st.anarchyHoldUnlimited = on;
    if (on) burstCooldownUntil = 0;
    else if (st.mode === "anarchy" && keyboardHold) {
      burstCooldownUntil = performance.now() + HOLD_COOLDOWN_MS;
      stopKeyboardHold();
    }
    const help = on ? "Anarchy repeats moves until release (unlimited hold is on for this run)." :
      "Anarchy repeats moves for up to 3 seconds, then pauses for 2 seconds; release and press again.";
    document.querySelectorAll("[data-hold-help]").forEach((el) => {
      el.textContent = `${help} Democracy renews your single vote until release. Shift votes on release.`;
    });
  }
  function keyboardCoolingDown() {
    return performance.now() < Math.max(burstCooldownUntil, serverCooldownUntil);
  }
  function stopKeyboardHold() {
    clearInterval(holdTimer);
    holdTimer = null; keyboardHold = null; shiftTarget = null;
    heldKeys.clear();
  }
  function startKeyboardHold(key, btn) {
    if (!st.connected || keyboardCoolingDown() || heldKeys.has(key)) return;
    heldKeys.set(key, btn);
    if (keyboardHold?.btn === btn) return; // aliases share one repeat stream
    const startedAt = keyboardHold?.startedAt || performance.now();
    keyboardHold = { btn, startedAt };
    castVote(btn);
    if (holdTimer) return;
    holdTimer = setInterval(() => {
      if (st.mode === "anarchy" && !st.anarchyHoldUnlimited && performance.now() - keyboardHold.startedAt >= HOLD_LIMIT_MS) {
        burstCooldownUntil = performance.now() + HOLD_COOLDOWN_MS;
        stopKeyboardHold();
      } else castVote(keyboardHold.btn);
    }, HOLD_MS);
  }
  function keyboardBlocked(e) {
    return e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey ||
      e.target instanceof Element && (e.target.isContentEditable ||
        e.target.closest("input, textarea, select, button, a, summary, [role='textbox']"));
  }
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Shift") shiftTarget = null;
    if (keyboardBlocked(e)) { stopKeyboardHold(); return; }
    // Wait for release: Shift used for Tab or another shortcut is not a Select vote.
    if (e.key === "Shift") {
      if (!e.repeat) shiftTarget = e.target;
      return;
    }
    if (e.key >= "1" && e.key <= "5") {
      if (!st.stepsOn) return;
      const n = STEP_CHOICES.includes(Number(e.key)) ? Number(e.key) : null;
      if (n) {
        e.preventDefault();
        if (!e.repeat) root.querySelector(`.steps button[data-steps="${n}"]`)?.click();
      }
      return;
    }
    const btn = KEYMAP[e.key] || KEYMAP[e.key.toLowerCase()];
    if (!btn) return;
    e.preventDefault();
    if (!e.repeat) startKeyboardHold(e.key.toLowerCase(), btn);
  });
  window.addEventListener("keyup", (e) => {
    const key = e.key.toLowerCase(), btn = heldKeys.get(key);
    heldKeys.delete(key);
    if (btn && keyboardHold?.btn === btn && ![...heldKeys.values()].includes(btn)) stopKeyboardHold();
    if (e.key !== "Shift") return;
    const standalone = shiftTarget === e.target && !keyboardBlocked(e);
    shiftTarget = null;
    if (standalone && st.connected && !keyboardCoolingDown()) castVote("select");
  });
  window.addEventListener("blur", stopKeyboardHold);
  document.addEventListener("visibilitychange", () => { if (document.hidden) stopKeyboardHold(); });
  document.addEventListener("focusin", (e) => { if (keyboardBlocked(e)) stopKeyboardHold(); });

  buildUI();
  paintStatus();
  connect();
  requestAnimationFrame(tick);
})();
