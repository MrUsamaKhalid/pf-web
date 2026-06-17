"""
PropertyFinder Photo ZIP Downloader - backend

Flask service that scrapes the photos from a PropertyFinder listing with a
headless Chromium browser (Playwright) and returns them bundled as a ZIP.

POST /scrape streams progress as newline-delimited JSON (NDJSON) so the
frontend can show a live log of the workflow. The final line carries the ZIP
(base64) plus the photo count and filename.

Stream message shapes
----------------------
{"type": "log",      "message": "..."}                       progress step
{"type": "progress", "done": 3, "total": 18, "message": ...} download progress
{"type": "error",    "message": "..."}                       fatal, stream ends
{"type": "done",     "count": 18, "filename": "...zip",
                     "zip": "<base64>"}                       success, stream ends
"""

import io
import os
import re
import json
import base64
import zipfile
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests

app = Flask(__name__)

# Allow the GitHub Pages frontend (any origin) to call us.
CORS(app, resources={r"/*": {"origins": "*"}})

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
TARGET_WIDTH = 1200          # preferred image width
MIN_BYTES = 8 * 1024         # reject anything smaller than 8 KB
MAX_IMAGES = 80              # safety cap on number of photos
REQUEST_TIMEOUT = 25         # per-image download timeout (seconds)
DOWNLOAD_WORKERS = 6         # parallel image downloads
NAV_TIMEOUT = 60_000         # page navigation timeout (ms)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.propertyfinder.ae/",
}

# Words that indicate an image is NOT a listing photo.
BAD_WORDS = [
    "logo", "avatar", "agent", "broker", "agency", "profile", "icon",
    "sprite", "placeholder", "default", "map", "badge", "watermark",
    "svg", "favicon", "tracking", "pixel",
]

# JSON keys whose string values are treated as image URLs even without a
# recognisable file extension.
IMAGE_FIELD_HINTS = {
    "url", "src", "image", "images", "photo", "photos", "thumbnail",
    "fullscreen", "full_screen", "original", "full", "large", "medium",
    "main", "cover", "picture",
}

# Query-string keys that only control rendering/size (stripped when grouping
# duplicate size variants together).
SIZE_QUERY_KEYS = {
    "w", "width", "h", "height", "q", "quality", "fit", "crop", "resize",
    "format", "fm", "dpr", "auto", "cs", "size", "rect",
}

IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|bmp|tiff?)(?:[?#]|$)", re.I)


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def normalize_protocol(url):
    """Turn a protocol-relative //host/path into https://host/path."""
    if url.startswith("//"):
        return "https:" + url
    return url


def looks_like_image_url(s):
    """Heuristic: does this string look like a downloadable image URL?"""
    if not isinstance(s, str) or len(s) < 8:
        return False
    if s.startswith("data:"):
        return False
    if not (s.startswith("http://") or s.startswith("https://") or s.startswith("//")):
        return False
    if IMG_EXT_RE.search(s):
        return True
    low = s.lower()
    if "/_next/image" in low and "url=" in low:
        return True
    return False


def _unwrap_next_image(url):
    """If url is a Next.js image-optimiser URL, return the decoded inner URL."""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if "/_next/image" in p.path or p.path.endswith("/image"):
        inner = parse_qs(p.query).get("url", [None])[0]
        if inner:
            return unquote(inner)
    return None


def is_listing_photo(url):
    """Filter out logos, avatars, icons, svgs, data URLs, etc."""
    low = url.lower()
    if low.startswith("data:"):
        return False
    if ".svg" in low:
        return False
    for word in BAD_WORDS:
        if word in low:
            return False
    return True


def upscale_url(url, target=TARGET_WIDTH):
    """
    Best-effort: rewrite common width markers in a URL to `target`.

    Handles ?width=800, &w=800, /800/, _800x600 and Cloudinary-style w_800.
    Never downsizes, and the caller always keeps the original as a fallback so
    signed URLs that reject the change still work.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return url
    changed = False

    if p.query:
        qs = parse_qs(p.query, keep_blank_values=True)
        for key in list(qs.keys()):
            if key.lower() in ("width", "w"):
                try:
                    current = int(re.sub(r"\D", "", qs[key][0]) or "0")
                except ValueError:
                    current = 0
                if current == 0 or current < target:
                    qs[key] = [str(target)]
                    changed = True
        if changed:
            p = p._replace(query=urlencode(qs, doseq=True))

    path = p.path

    def _bump_wxh(m):
        w, h = int(m.group("w")), int(m.group("h"))
        if w >= target:
            return m.group(0)
        new_h = max(1, round(h * target / w))
        return f"{m.group('sep')}{target}x{new_h}"

    new_path = re.sub(
        r"(?P<sep>[_/])(?P<w>\d{2,4})x(?P<h>\d{2,4})", _bump_wxh, path
    )

    def _bump_dim(m):
        val = int(m.group("n"))
        if val >= target:
            return m.group(0)
        return f"{m.group('k')}_{target}"

    new_path = re.sub(
        r"(?<![A-Za-z])(?P<k>[wh])_(?P<n>\d{2,4})", _bump_dim, new_path
    )

    def _bump_segment(m):
        val = int(m.group(1))
        if 100 <= val < target:
            return f"/{target}/"
        return m.group(0)

    new_path = re.sub(r"/(\d{3,4})/", _bump_segment, new_path)

    if new_path != path:
        p = p._replace(path=new_path)
        changed = True

    return urlunparse(p) if changed else url


def canonical_key(url):
    """A key identical for size variants of the same image (for grouping)."""
    inner = _unwrap_next_image(url)
    if inner:
        url = normalize_protocol(inner)
    try:
        p = urlparse(url)
    except ValueError:
        return url.lower()

    query = ""
    if p.query:
        qs = parse_qs(p.query, keep_blank_values=True)
        qs = {k: v for k, v in qs.items() if k.lower() not in SIZE_QUERY_KEYS}
        query = urlencode(qs, doseq=True)

    path = p.path
    path = re.sub(r"([_/])\d{2,4}x\d{2,4}", r"\1<s>", path)
    path = re.sub(r"(?<![A-Za-z])([wh])_\d{2,4}", r"\1_<s>", path)
    path = re.sub(r"/\d{3,4}(?=/)", "/<s>", path)

    return f"{p.netloc.lower()}{path.lower()}?{query.lower()}"


def candidates_for(url):
    """Ordered (tier, url) download candidates for one source URL."""
    url = normalize_protocol(url)
    inner = _unwrap_next_image(url)
    base = normalize_protocol(inner) if inner else url

    tiers = []
    up_base = upscale_url(base)
    if up_base != base:
        tiers.append((0, up_base))
    if inner:
        up_opt = upscale_url(url)
        if up_opt != url:
            tiers.append((1, up_opt))
        tiers.append((1, base))
    tiers.append((2, base))
    if url != base:
        tiers.append((3, url))
    return tiers


def build_download_plan(urls):
    """Group size variants and return ordered candidate lists per photo."""
    groups = {}
    order = []
    for u in urls:
        u = normalize_protocol(u)
        key = canonical_key(u)
        if key not in groups:
            groups[key] = {}
            order.append(key)
        for tier, cand in candidates_for(u):
            if cand not in groups[key] or tier < groups[key][cand]:
                groups[key][cand] = tier

    plans = []
    for key in order:
        ordered = sorted(groups[key].items(), key=lambda kv: kv[1])
        plans.append([c for c, _ in ordered])
        if len(plans) >= MAX_IMAGES:
            break
    return plans


def slug_from_url(url):
    """Build a filename slug from the listing URL path."""
    try:
        path = urlparse(url).path
    except ValueError:
        return "propertyfinder-photos"

    segments = [s for s in path.split("/") if s]
    if not segments:
        return "propertyfinder-photos"

    last = segments[-1]
    last = re.sub(r"\.\w+$", "", last)
    last = last.lower()
    last = re.sub(r"[\s_]+", "-", last)
    last = re.sub(r"[^a-z0-9\-]", "", last)
    last = re.sub(r"-\d+$", "", last)
    last = re.sub(r"-{2,}", "-", last)
    last = last.strip("-")

    return last or "propertyfinder-photos"


# --------------------------------------------------------------------------- #
# __NEXT_DATA__ JSON scanning
# --------------------------------------------------------------------------- #
def scan_json_for_images(node, found, key_hint=None):
    """Recursively collect image-looking URLs from arbitrary JSON."""
    if isinstance(node, dict):
        for k, v in node.items():
            scan_json_for_images(v, found, key_hint=str(k).lower())
    elif isinstance(node, list):
        for item in node:
            scan_json_for_images(item, found, key_hint=key_hint)
    elif isinstance(node, str):
        if looks_like_image_url(node):
            found.add(node)
        elif key_hint in IMAGE_FIELD_HINTS and node.startswith(
            ("http://", "https://", "//")
        ):
            found.add(node)


# --------------------------------------------------------------------------- #
# Browser scraping (Playwright)
# --------------------------------------------------------------------------- #
DOM_COLLECT_JS = r"""
() => {
  const urls = new Set();

  const pushSrcset = (ss) => {
    if (!ss) return;
    let best = null, bestW = -1;
    ss.split(',').forEach(part => {
      const seg = part.trim().split(/\s+/);
      const u = seg[0];
      let w = 0;
      if (seg[1]) {
        const m = seg[1].match(/(\d+)(w|x)/);
        if (m) w = parseInt(m[1], 10);
      }
      if (u && w >= bestW) { bestW = w; best = u; }
    });
    if (best) urls.add(best);
  };

  document.querySelectorAll('img').forEach(img => {
    if (img.currentSrc) urls.add(img.currentSrc);
    if (img.src) urls.add(img.src);
    ['data-src', 'data-lazy-src'].forEach(a => {
      const v = img.getAttribute(a);
      if (v) urls.add(v);
    });
    pushSrcset(img.getAttribute('srcset'));
    pushSrcset(img.getAttribute('data-srcset'));
  });

  document.querySelectorAll('source').forEach(s => {
    pushSrcset(s.getAttribute('srcset'));
    pushSrcset(s.getAttribute('data-srcset'));
  });

  document.querySelectorAll('[style*="background"]').forEach(el => {
    const bg = getComputedStyle(el).backgroundImage;
    if (bg && bg.includes('url(')) {
      const m = bg.match(/url\(["']?(.*?)["']?\)/);
      if (m && m[1]) urls.add(m[1]);
    }
  });

  return Array.from(urls);
}
"""

AUTO_SCROLL_JS = r"""
async () => {
  await new Promise((resolve) => {
    let total = 0;
    const step = 700;
    const timer = setInterval(() => {
      window.scrollBy(0, step);
      total += step;
      if (total >= document.body.scrollHeight + 2500) {
        clearInterval(timer);
        resolve();
      }
    }, 180);
  });
}
"""


def scrape_listing_stream(url):
    """
    Generator that drives the browser and yields progress as tuples:
        ("log", message)     human-readable step
        ("result", url_set)  final set of raw image URLs (last item)
    """
    # Imported here so the module still imports cleanly if the browser binary
    # is not yet installed; required only when actually scraping.
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    image_urls = set()
    next_data_text = None

    yield ("log", "Launching headless browser")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = browser.new_context(
                user_agent=BROWSER_HEADERS["User-Agent"],
                locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()

            yield ("log", "Opening the listing page")
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except PWTimeout:
                pass

            yield ("log", "Scrolling to load all gallery photos")
            try:
                page.evaluate(AUTO_SCROLL_JS)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            try:
                page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
            page.wait_for_timeout(500)

            yield ("log", "Reading embedded gallery data")
            try:
                next_data_text = page.eval_on_selector(
                    "#__NEXT_DATA__", "el => el.textContent"
                )
            except Exception:
                next_data_text = None
            if not next_data_text:
                try:
                    html = page.content()
                    m = re.search(
                        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                        html,
                        re.S,
                    )
                    if m:
                        next_data_text = m.group(1)
                except Exception:
                    next_data_text = None

            try:
                for u in page.evaluate(DOM_COLLECT_JS):
                    if u:
                        image_urls.add(u)
            except Exception:
                pass
        finally:
            browser.close()

    if next_data_text:
        try:
            scan_json_for_images(json.loads(next_data_text), image_urls)
        except (ValueError, TypeError):
            pass

    yield ("log", f"Found {len(image_urls)} image links on the page")
    yield ("result", image_urls)


# --------------------------------------------------------------------------- #
# Downloading + ZIP packaging
# --------------------------------------------------------------------------- #
def _ext_for(content, content_type):
    """Pick a file extension; default jpg unless clearly png/webp."""
    c = (content_type or "").lower()
    if "png" in c:
        return "png"
    if "webp" in c:
        return "webp"
    if "jpeg" in c or "jpg" in c:
        return "jpg"
    if content[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def download_group(candidates):
    """Try each candidate URL until one downloads as a >= 8 KB image."""
    for url in candidates:
        try:
            r = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code == 200 and r.content and len(r.content) >= MIN_BYTES:
            ctype = r.headers.get("Content-Type", "")
            if "image" in ctype.lower() or _ext_for(r.content, ctype):
                return r.content, ctype
    return None


def build_zip(photos):
    """Bundle downloaded photos into an in-memory ZIP."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for index, (content, ctype) in enumerate(photos, start=1):
            ext = _ext_for(content, ctype)
            zf.writestr(f"photo_{index:02d}.{ext}", content)
    buffer.seek(0)
    return buffer


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "propertyfinder-photo-downloader"})


@app.route("/scrape", methods=["POST", "OPTIONS"])
def scrape():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "Missing 'url' in request body."}), 400
    if "propertyfinder" not in url.lower():
        return jsonify({"error": "URL must be a PropertyFinder listing."}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    def nd(obj):
        return json.dumps(obj) + "\n"

    def generate():
        try:
            image_urls = set()
            try:
                for kind, payload in scrape_listing_stream(url):
                    if kind == "log":
                        yield nd({"type": "log", "message": payload})
                    elif kind == "result":
                        image_urls = payload
            except Exception:
                yield nd({
                    "type": "error",
                    "message": "Could not load the listing page. It may be slow "
                               "or blocking automated access - try again.",
                })
                return

            yield nd({"type": "log",
                      "message": "Filtering out logos, icons and non-photos"})
            filtered = [
                u for u in (normalize_protocol(x) for x in image_urls)
                if is_listing_photo(u)
            ]
            plans = build_download_plan(filtered)
            if not plans:
                yield nd({"type": "error",
                          "message": "No photos found on this listing."})
                return

            total = len(plans)
            yield nd({"type": "log",
                      "message": f"Preparing to download {total} photos"})

            results = [None] * total
            done = 0
            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                fut_to_index = {
                    pool.submit(download_group, plan): i
                    for i, plan in enumerate(plans)
                }
                yield nd({"type": "progress", "done": 0, "total": total,
                          "message": f"Downloading photos 0/{total}"})
                for fut in as_completed(fut_to_index):
                    i = fut_to_index[fut]
                    try:
                        results[i] = fut.result()
                    except Exception:
                        results[i] = None
                    done += 1
                    yield nd({"type": "progress", "done": done, "total": total,
                              "message": f"Downloading photos {done}/{total}"})

            photos = []
            seen = set()
            for result in results:
                if not result:
                    continue
                content, ctype = result
                digest = hashlib.sha256(content).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                photos.append((content, ctype))

            if not photos:
                yield nd({"type": "error",
                          "message": "No photos could be downloaded from this listing."})
                return

            yield nd({"type": "log",
                      "message": f"{len(photos)} unique photos kept (duplicates removed)"})
            yield nd({"type": "log", "message": "Packaging ZIP"})

            zip_buffer = build_zip(photos)
            filename = f"{slug_from_url(url)}.zip"
            encoded = base64.b64encode(zip_buffer.getvalue()).decode("ascii")
            yield nd({
                "type": "done",
                "count": len(photos),
                "filename": filename,
                "zip": encoded,
            })
        except Exception:
            yield nd({"type": "error",
                      "message": "Something went wrong while building the ZIP."})

    return Response(
        generate(),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Not found."}), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"error": "Method not allowed. Use POST /scrape."}), 405


@app.errorhandler(500)
def server_error(_):
    return jsonify({"error": "Server error while processing the listing."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
