/**
 * KLIPEK TV — dynamic HLS live channel
 *
 * The Worker does NOT transcode video.
 * GitHub Actions preprocesses every source MP4 into HLS segments
 * and publishes a manifest + segments to the Hugging Face dataset.
 *
 * Public channel:
 *   /live.m3u8
 *
 * Optional:
 *   /                     player page
 *   /api/status           channel status
 *   /api/manifest         generated manifest
 *   /health
 */

const CFG = {
  HF_OWNER: "androjid21",
  HF_DATASET: "klippek",
  HF_BRANCH: "main",

  // Folder produced by the GitHub Action.
  HLS_ROOT: "hls",

  // Number of clips kept in the local anti-repeat window.
  HISTORY_SIZE: 30,

  // How many clips are exposed ahead of the current live point.
  PLAYLIST_CLIPS: 5,

  // Cache the manifest briefly. New uploads will appear after this.
  MANIFEST_CACHE_MS: 60_000,

  // The HLS segments themselves are public HF files.
  SEGMENT_CACHE_CONTROL: "public, max-age=31536000, immutable",
};

let manifestCache = {
  data: null,
  expires: 0,
};

function json(data, status = 200, extra = {}) {
  const headers = new Headers({
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    ...extra,
  });
  return new Response(JSON.stringify(data, null, 2), { status, headers });
}

function encodeHFPath(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

function hfUrl(path) {
  return `https://huggingface.co/datasets/${CFG.HF_OWNER}/${CFG.HF_DATASET}/resolve/${CFG.HF_BRANCH}/${encodeHFPath(path)}?download=true`;
}

function parseCookie(request, name) {
  const raw = request.headers.get("Cookie") || "";
  for (const item of raw.split(";")) {
    const [k, ...v] = item.trim().split("=");
    if (k === name) return decodeURIComponent(v.join("="));
  }
  return "";
}

function readHistory(request, count) {
  try {
    const value = parseCookie(request, "klip_history");
    const arr = JSON.parse(value || "[]");
    if (!Array.isArray(arr)) return [];
    return arr.filter(Number.isInteger).filter(i => i >= 0 && i < count).slice(0, CFG.HISTORY_SIZE);
  } catch {
    return [];
  }
}

function cookie(history) {
  return [
    `klip_history=${encodeURIComponent(JSON.stringify(history))}`,
    "Path=/",
    "Max-Age=2592000",
    "SameSite=Lax",
  ].join("; ");
}

function secureRandomInt(max) {
  if (max <= 1) return 0;
  const n = crypto.getRandomValues(new Uint32Array(1))[0];
  return n % max;
}

function shuffled(a) {
  const x = a.slice();
  for (let i = x.length - 1; i > 0; i--) {
    const j = secureRandomInt(i + 1);
    [x[i], x[j]] = [x[j], x[i]];
  }
  return x;
}

async function getManifest(force = false) {
  const now = Date.now();

  if (!force && manifestCache.data && manifestCache.expires > now) {
    return manifestCache.data;
  }

  const url = hfUrl(`${CFG.HLS_ROOT}/manifest.json`);
  const response = await fetch(url, {
    headers: { "accept": "application/json" },
    cf: { cacheTtl: 60, cacheEverything: true },
  });

  if (!response.ok) {
    throw new Error(`Hugging Face manifest HTTP ${response.status}`);
  }

  const data = await response.json();

  if (!Array.isArray(data.clips) || !data.clips.length) {
    throw new Error("A HLS manifest üres.");
  }

  manifestCache = {
    data,
    expires: now + CFG.MANIFEST_CACHE_MS,
  };

  return data;
}

/*
 * Build a deterministic-but-changing channel schedule from UTC epoch.
 *
 * Every playlist request uses the current 6-second HLS segment position.
 * The sequence is derived from a 64-bit-ish numeric seed. The same live
 * position therefore remains stable for all viewers without a database.
 *
 * We intentionally use a large clip cycle and rotate it periodically.
 */
function seededNumber(seed) {
  let x = Number(seed % 2147483647n);
  x = (x * 48271) % 2147483647;
  return x / 2147483647;
}

function makeOrder(count, epochBucket) {
  const arr = Array.from({ length: count }, (_, i) => i);

  // Several independent deterministic swaps.
  let seed = BigInt(epochBucket) * 1000003n + 9176n;
  for (let i = arr.length - 1; i > 0; i--) {
    seed = (seed * 1103515245n + 12345n) & 0x7fffffffn;
    const j = Number(seed % BigInt(i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

function clipDuration(clip) {
  const d = Number(clip.duration);
  return Number.isFinite(d) && d > 0 ? d : 1;
}

function totalDuration(manifest) {
  return manifest.clips.reduce((s, c) => s + clipDuration(c), 0);
}

function locateTime(manifest, seconds) {
  const clips = manifest.clips;
  let t = ((seconds % totalDuration(manifest)) + totalDuration(manifest)) % totalDuration(manifest);

  // Use a slowly rotating deterministic order.
  const rotation = Math.floor(seconds / 21600); // changes every 6 hours
  const order = makeOrder(clips.length, rotation);

  for (let p = 0; p < order.length; p++) {
    const idx = order[p];
    const d = clipDuration(clips[idx]);
    if (t < d) {
      return {
        order,
        position: p,
        clipIndex: idx,
        offset: t,
      };
    }
    t -= d;
  }

  const idx = order[order.length - 1];
  return { order, position: order.length - 1, clipIndex: idx, offset: 0 };
}

function clipSegments(clip) {
  if (Array.isArray(clip.segments)) return clip.segments;
  return [];
}

function makeMediaPlaylist(manifest, nowSeconds) {
  const clips = manifest.clips;
  const total = totalDuration(manifest);

  if (!clips.length || total <= 0) {
    throw new Error("Nincs lejátszható klip.");
  }

  const located = locateTime(manifest, nowSeconds);

  // Current clip + several future clips.
  const selected = [];
  for (let n = 0; n < Math.min(CFG.PLAYLIST_CLIPS, located.order.length); n++) {
    const pos = (located.position + n) % located.order.length;
    selected.push(located.order[pos]);
  }

  // HLS media sequence is based on a stable epoch bucket.
  const seq = Math.floor(nowSeconds / 6);

  const lines = [
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-TARGETDURATION:6",
    `#EXT-X-MEDIA-SEQUENCE:${seq}`,
    "#EXT-X-INDEPENDENT-SEGMENTS",
  ];

  let remainingSkip = located.offset;
  let first = true;

  for (const clipIndex of selected) {
    const clip = clips[clipIndex];
    const segments = clipSegments(clip);

    if (!segments.length) continue;

    // If this is the live/current clip, skip segments until current offset.
    let startAt = 0;
    if (first) {
      let sum = 0;
      for (let i = 0; i < segments.length; i++) {
        const d = Number(segments[i].duration) || 0;
        if (sum + d > remainingSkip) {
          startAt = i;
          break;
        }
        sum += d;
      }
    }

    lines.push("#EXT-X-DISCONTINUITY");

    for (let i = startAt; i < segments.length; i++) {
      const s = segments[i];
      const duration = Number(s.duration) || 1;
      const path = s.path;

      if (!path) continue;

      lines.push(`#EXTINF:${duration.toFixed(3)},`);
      lines.push(hfUrl(path));
    }

    first = false;
  }

  // This is a live playlist: deliberately NO EXT-X-ENDLIST.
  return lines.join("\n") + "\n";
}

const PLAYER_HTML = `<!doctype html>
<html lang="hu">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#070910">
<title>KLIPEK TV</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>
<style>
*{box-sizing:border-box}html,body{margin:0;background:#070910;color:#fff;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial}
body{min-height:100vh;display:grid;place-items:center}
main{width:min(1100px,100%);padding:20px}
.card{background:#10131c;border:1px solid #252a38;border-radius:22px;overflow:hidden;box-shadow:0 25px 80px #0008}
.top{padding:17px 20px;display:flex;justify-content:space-between;align-items:center}
.logo{font-weight:900;letter-spacing:.14em}.live{font-size:12px;color:#8fe8b2}
video{width:100%;display:block;background:#000;aspect-ratio:16/9}
.bottom{padding:16px 20px;color:#9ca6ba;font-size:13px}
</style>
</head>
<body>
<main><section class="card">
<div class="top"><div class="logo">KLIPEK TV</div><div class="live" id="status">● LIVE</div></div>
<video id="video" controls autoplay playsinline></video>
<div class="bottom">Élő zenei klipcsatorna • automatikusan kevert műsor</div>
</section></main>
<script>
const video=document.getElementById("video");
const status=document.getElementById("status");
const src="/live.m3u8";

if (video.canPlayType("application/vnd.apple.mpegurl")) {
  video.src=src;
  video.play().catch(()=>{});
} else if (window.Hls && Hls.isSupported()) {
  const hls=new Hls({
    liveSyncDurationCount:3,
    maxLiveSyncPlaybackRate:1.2,
    enableWorker:true,
    lowLatencyMode:false
  });
  hls.loadSource(src);
  hls.attachMedia(video);
  hls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{}));
  hls.on(Hls.Events.ERROR,(e,d)=>{
    if(d.fatal){
      status.textContent="● ÚJRACSATLAKOZÁS";
      setTimeout(()=>hls.startLoad(),1200);
    }
  });
} else {
  status.textContent="A böngésző nem támogatja a HLS-t";
}
</script>
</body>
</html>`;

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET,HEAD,OPTIONS",
        },
      });
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      return new Response(PLAYER_HTML, {
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-store",
        },
      });
    }

    if (url.pathname === "/health") {
      return new Response("KLIPEK TV OK", {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    if (url.pathname === "/api/manifest") {
      try {
        const manifest = await getManifest(url.searchParams.get("refresh") === "1");
        return json(manifest);
      } catch (e) {
        return json({ ok:false, error:e.message }, 502);
      }
    }

    if (url.pathname === "/api/now") {
      try {
        const manifest = await getManifest(false);
        const nowSeconds = Date.now() / 1000;
        const located = locateTime(manifest, nowSeconds);
        const clip = manifest.clips[located.clipIndex];

        return json({
          ok: true,
          channel: "KLIPEK TV",
          title: clip?.title || clip?.source || "KLIPEK TV",
          source: clip?.source || null,
          clipIndex: located.clipIndex,
          offsetSeconds: Math.floor(located.offset),
          durationSeconds: Math.round(clipDuration(clip)),
          playlist: "/live.m3u8",
          updatedAt: manifest.generatedAt || null,
        });
      } catch (e) {
        return json({ ok:false, error:e.message }, 502);
      }
    }

    if (url.pathname === "/api/status") {
      try {
        const manifest = await getManifest(false);
        return json({
          ok: true,
          channel: "KLIPEK TV",
          dataset: `${CFG.HF_OWNER}/${CFG.HF_DATASET}`,
          clips: manifest.clips.length,
          totalDurationSeconds: Math.round(totalDuration(manifest)),
          totalDurationHours: +(totalDuration(manifest) / 3600).toFixed(2),
          playlist: "/live.m3u8",
          updatedAt: manifest.generatedAt || null,
        });
      } catch (e) {
        return json({ ok:false, error:e.message }, 502);
      }
    }

    if (url.pathname === "/live.m3u8") {
      try {
        const manifest = await getManifest(false);

        // Use a common epoch so every viewer sees the same "station".
        const nowSeconds = Date.now() / 1000;
        const playlist = makeMediaPlaylist(manifest, nowSeconds);

        return new Response(playlist, {
          headers: {
            "content-type": "application/vnd.apple.mpegurl; charset=utf-8",
            "cache-control": "no-store, no-cache, must-revalidate, max-age=0",
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET,HEAD,OPTIONS",
            "access-control-allow-headers": "*",
          },
        });
      } catch (e) {
        return new Response(`#EXTM3U\n# KLIPEK TV HIBA\n# ${String(e.message).replace(/\n/g," ")}\n`, {
          status: 503,
          headers: {
            "content-type": "application/vnd.apple.mpegurl; charset=utf-8",
            "cache-control": "no-store",
          },
        });
      }
    }

    return new Response("404 - KLIPEK TV", { status:404 });
  }
};
