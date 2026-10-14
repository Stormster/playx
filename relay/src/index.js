// PlayX live relay: one stream in from the home server, fanned out to every viewer.
//
//   home server ──wss /pub (secret)──▶ Hub Durable Object ──wss /ws──▶ viewers
//
// Wire format. Packets are the same bytes as in R2 chunks:
//   [kind u8: 1 key, 2 delta][audioLen u16][seq u32][mu-law audio][zlib RGB565 frame or XOR delta]
// A "batch" is [u32 len][packet] repeated.
//
// home -> hub:   0x10 batch          live packets
//                0x11                game froze (idle, empty, host pause)
// hub -> viewer: 0x01 batch          live packets
//                0x02 batch          catch-up: keyframe + deltas so far, continues into live
//                0x03                game froze, a gap now is a pause
//                0x04 batch          still picture from storage (not continuous with live)
// hub -> home:   text {"viewers":N}  every ~5 s while live
//
// Cost notes (Workers free plan): outgoing WebSocket messages are free, incoming ones bill at
// 20:1, and with the Hibernation API an idle hub (frozen game) bills no duration.

import { DurableObject } from "cloudflare:workers";

const T_LIVE_IN = 0x10, T_END_IN = 0x11;
const T_LIVE = 0x01, T_CATCHUP = 0x02, T_END = 0x03, T_STILL = 0x04;
const MAX_GOP = 240;                  // packets kept for late joiners (keyframes arrive every 60)
const MAX_STORED = 1_500_000;         // bytes; the still picture saved when the game freezes

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/healthz") return new Response("ok");
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("PlayX live relay", { status: 426 });
    }
    const run = (url.searchParams.get("run") || "firered").replace(/[^a-z0-9_-]/gi, "").slice(0, 40);

    if (url.pathname === "/pub") {
      const auth = request.headers.get("authorization") || "";
      if (!env.PUB_SECRET || !safeEqual(auth, `Bearer ${env.PUB_SECRET}`)) {
        return new Response("forbidden", { status: 403 });
      }
    } else if (url.pathname === "/ws") {
      const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
      const origin = request.headers.get("origin") || "";
      if (allowed.length && !allowed.includes(origin)) {
        return new Response("forbidden", { status: 403 });
      }
    } else {
      return new Response("not found", { status: 404 });
    }
    const hub = env.HUB.get(env.HUB.idFromName(run));
    return hub.fetch(request);
  },
};

function safeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a), y = enc.encode(b);
  if (x.length !== y.length) return false;
  return crypto.subtle.timingSafeEqual(x, y);
}

function packBatch(type, packets) {
  let size = 1;
  for (const p of packets) size += 4 + p.length;
  const out = new Uint8Array(size);
  const dv = new DataView(out.buffer);
  out[0] = type;
  let off = 1;
  for (const p of packets) {
    dv.setUint32(off, p.length, true);
    out.set(p, off + 4);
    off += 4 + p.length;
  }
  return out;
}

function* unpackBatch(bytes) {
  const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let off = 1;
  while (off + 4 <= bytes.length) {
    const len = dv.getUint32(off, true);
    off += 4;
    if (off + len > bytes.length) return;
    yield bytes.subarray(off, off + len);
    off += len;
  }
}

export class Hub extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // In memory only while the stream is live. A hibernated hub wakes up with these empty.
    this.gop = null;          // packets since the last keyframe, continuous with what's being sent
    this.frozen = false;
    this.batches = 0;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    if (url.pathname === "/pub") {
      for (const old of this.ctx.getWebSockets("pub")) {
        try { old.close(1000, "replaced"); } catch {}
      }
      this.ctx.acceptWebSocket(server, ["pub"]);
      this.gop = null;        // the publisher always starts with a keyframe
      this.frozen = false;
    } else {
      this.ctx.acceptWebSocket(server, ["viewer"]);
      if (this.gop && this.gop.length) {
        server.send(packBatch(T_CATCHUP, this.gop));
        if (this.frozen) server.send(Uint8Array.of(T_END));
      } else {
        const still = await this.ctx.storage.get("still");
        if (still) server.send(still);
      }
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(ws, message) {
    if (typeof message === "string") return;                 // viewers have nothing to say here
    if (!this.ctx.getTags(ws).includes("pub")) return;
    const bytes = new Uint8Array(message);
    if (bytes[0] === T_LIVE_IN) {
      this.frozen = false;
      for (const p of unpackBatch(bytes)) {
        if (p[0] === 1) this.gop = [p.slice()];
        else if (this.gop && this.gop.length < MAX_GOP) this.gop.push(p.slice());
      }
      bytes[0] = T_LIVE;
      this.broadcast(bytes);
      if (++this.batches % 50 === 0) {
        try { ws.send(JSON.stringify({ viewers: this.ctx.getWebSockets("viewer").length })); } catch {}
      }
    } else if (bytes[0] === T_END_IN) {
      this.frozen = true;
      this.broadcast(Uint8Array.of(T_END));
      if (this.gop && this.gop.length) {
        const still = packBatch(T_STILL, this.gop);
        if (still.length <= MAX_STORED) await this.ctx.storage.put("still", still);
      }
      try { ws.send(JSON.stringify({ viewers: this.ctx.getWebSockets("viewer").length })); } catch {}
    }
  }

  broadcast(data) {
    for (const v of this.ctx.getWebSockets("viewer")) {
      try { v.send(data); } catch {}
    }
  }

  async webSocketClose(ws, code, reason) {
    try { ws.close(code === 1005 ? 1000 : code, reason); } catch {}
  }

  async webSocketError(ws) {
    try { ws.close(1011, "error"); } catch {}
  }
}
