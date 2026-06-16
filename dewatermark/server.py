"""Resident Pro-video watermark-removal service (ProPainter).

Loads ProPainter models ONCE into GPU memory and serves de-watermark requests.
Pipeline: probe resolution -> crop the fixed watermark region -> ProPainter video
inpainting (cached models) -> ffmpeg alpha-overlay composite -> write output.

Resolution support: the Gemini/Flow 720-tier sparkle sits 96px from the bottom-right
corner (48px) in BOTH orientations, so the same mask works at a transposed crop offset
of (W-216, H-216). 1280x720 (landscape) and 720x1280 (portrait) are supported; any other
resolution returns {ok:false} so the caller falls back to the original (unprocessed) video.

Run with a torch+CUDA Python (e.g. the ComfyUI venv):
    PROPAINTER_DIR=/path/to/ProPainter /opt/ComfyUI/.venv/bin/python dewatermark/server.py

Env: PROPAINTER_DIR (required), WM_MASK_DIR, WM_PORT (18290), WM_CROP_SIZE (192),
     WM_INSET (216), WM_RAFT_ITER (12), WM_CRF (14), WM_ALLOWED_DIR (path-safety allowlist),
     WM_MAX_BODY (65536).

HTTP: GET /health ; POST /dewatermark {input, output}  (local file paths)
"""
import sys
import os
import json
import time
import subprocess
import tempfile
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROPAINTER_DIR = os.environ.get("PROPAINTER_DIR", "")
MASK_DIR = os.environ.get("WM_MASK_DIR", os.path.join(HERE, "masks"))
PORT = int(os.environ.get("WM_PORT", "18290"))
RAFT_ITER = os.environ.get("WM_RAFT_ITER", "12")
CRF = os.environ.get("WM_CRF", "14")
CROP_SIZE = int(os.environ.get("WM_CROP_SIZE", "192"))
INSET = int(os.environ.get("WM_INSET", "216"))               # crop top-left = (W-INSET, H-INSET)
ALLOWED_DIR = os.environ.get("WM_ALLOWED_DIR", "")           # if set, in/out must live under it
MAX_BODY = int(os.environ.get("WM_MAX_BODY", "65536"))
MASK = os.path.join(MASK_DIR, "mask192.png")
MASK_ALPHA = os.path.join(MASK_DIR, "mask192_alpha.png")
SUPPORTED = {(1280, 720), (720, 1280)}                       # 720-tier landscape/portrait

if not PROPAINTER_DIR or not os.path.isdir(PROPAINTER_DIR):
    sys.exit(f"PROPAINTER_DIR not set or invalid: {PROPAINTER_DIR!r}")
for p in (MASK, MASK_ALPHA):
    if not os.path.exists(p):
        sys.exit(f"missing mask: {p}")

sys.path.insert(0, PROPAINTER_DIR)
os.chdir(PROPAINTER_DIR)                                      # ProPainter resolves weights/ from cwd
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
import inference_propainter as ip                              # noqa: E402  (must have main())

if not hasattr(ip, "main"):
    sys.exit("inference_propainter has no main(); apply the def-main wrapper (see README).")

# ---- cache the 3 ProPainter models so they load once and stay resident ----
_cache = {}
_cache_lock = threading.Lock()
def _cached(name, orig):
    def f(*a, **k):
        with _cache_lock:                                    # guard concurrent first-load
            if name not in _cache:
                _cache[name] = orig(*a, **k)
            return _cache[name]
    return f
ip.RAFT_bi = _cached("raft", ip.RAFT_bi)
ip.RecurrentFlowCompleteNet = _cached("flow", ip.RecurrentFlowCompleteNet)
ip.InpaintGenerator = _cached("pp", ip.InpaintGenerator)

_gpu_lock = threading.Lock()


class Unsupported(Exception):
    """Resolution not calibrated -> caller should fall back, not treat as error."""


def _probe_size(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    w, h = (int(x) for x in out.split("x")[:2])
    return w, h


def _path_ok(p):
    if not ALLOWED_DIR:
        return True
    try:
        base = os.path.realpath(ALLOWED_DIR)
        return os.path.realpath(p).startswith(base + os.sep)
    except Exception:
        return False


def dewatermark(inp, out):
    """Remove the watermark from local video `inp`, write de-watermarked video to `out`."""
    w, h = _probe_size(inp)
    if (w, h) not in SUPPORTED:
        raise Unsupported(f"resolution {w}x{h} not calibrated (only 1280x720 / 720x1280)")
    cx, cy = w - INSET, h - INSET                            # crop top-left (watermark at (72,72))
    if cx < 0 or cy < 0:
        raise Unsupported(f"crop offset out of bounds for {w}x{h}")

    tmp = tempfile.mkdtemp(prefix="wm_", dir="/tmp")
    timings = {}
    try:
        cdir = os.path.join(tmp, "crop")
        os.makedirs(cdir)
        s = time.monotonic()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", inp,
                        "-vf", f"crop={CROP_SIZE}:{CROP_SIZE}:{cx}:{cy}", os.path.join(cdir, "f_%04d.png")],
                       check=True)
        timings["crop"] = round(time.monotonic() - s, 2)

        ppout = os.path.join(tmp, "pp")
        # NOTE: sys.argv mutation + ip.main() must stay inside _gpu_lock (do_POST holds it).
        sys.argv = ["x", "-i", cdir, "-m", MASK, "-o", ppout,
                    "--raft_iter", RAFT_ITER, "--fp16", "--mask_dilation", "4"]
        s = time.monotonic()
        ip.main()
        timings["propaint"] = round(time.monotonic() - s, 2)

        inpaint = os.path.join(ppout, "crop", "inpaint_out.mp4")
        if not os.path.exists(inpaint):
            raise RuntimeError("ProPainter produced no inpaint_out.mp4")
        s = time.monotonic()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", inp, "-i", inpaint, "-loop", "1", "-i", MASK_ALPHA,
                        "-filter_complex",
                        f"[2:v]format=gray[m];[1:v][m]alphamerge[fg];[0:v][fg]overlay={cx}:{cy}:shortest=1",
                        "-c:v", "libx264", "-crf", CRF, "-pix_fmt", "yuv420p", out], check=True)
        timings["composite"] = round(time.monotonic() - s, 2)
        if not os.path.exists(out):
            raise RuntimeError("composite produced no output")
        timings["resolution"] = f"{w}x{h}"
        return timings
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "models_loaded": sorted(_cache.keys())})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/dewatermark":
            return self._send(404, {"error": "not found"})
        ln = int(self.headers.get("Content-Length", 0) or 0)
        if ln <= 0 or ln > MAX_BODY:
            return self._send(400, {"error": "missing/oversized body"})
        try:
            req = json.loads(self.rfile.read(ln))
        except Exception:
            return self._send(400, {"error": "bad json"})
        inp, out = req.get("input"), req.get("output")
        if not inp or not out or not os.path.exists(inp):
            return self._send(400, {"error": "need valid existing 'input' and 'output' paths"})
        if not _path_ok(inp) or not _path_ok(out):
            return self._send(403, {"error": "path outside WM_ALLOWED_DIR"})
        with _gpu_lock:                                      # one GPU job at a time
            try:
                s = time.monotonic()
                timings = dewatermark(inp, out)
                self._send(200, {"ok": True, "output": out, "timings": timings,
                                 "total": round(time.monotonic() - s, 2)})
            except Unsupported as u:
                self._send(200, {"ok": False, "reason": str(u)})   # clean skip -> caller falls back
            except Exception as e:
                self._send(500, {"error": str(e)[:400]})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"[dewatermark] PROPAINTER_DIR={PROPAINTER_DIR} crop={CROP_SIZE} inset={INSET} "
          f"port={PORT} allowed_dir={ALLOWED_DIR or '(none)'} | models lazy-load on first request",
          flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
