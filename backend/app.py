"""
PropertyFinder Photo ZIP Downloader - backend

Reads a PropertyFinder listing's __NEXT_DATA__ (server-rendered JSON) and pulls
the FULL-resolution gallery photos, split into the same tabs the site shows:

    property[]   -> the listing's own photos   -> property/  in the ZIP
    community[]  -> community / area photos     -> community/ in the ZIP

Reliability:
  * Listing data is read by plain HTTP first (fast); headless Chromium is a
    fallback for bot-checked pages. The HTTP read itself is retried.
  * Each photo is downloaded with multiple passes: it retries the full-res URL,
    then falls back to the medium rendition, and every result is validated as a
    real image (magic bytes), so broken/blank files never reach the ZIP.

POST /scrape streams newline-delimited JSON (NDJSON) progress; the final "done"
message carries the ZIP (base64) plus property / community / total / failed.
GET / and GET /health return a status payload.
"""

import io
import os
import re
import time
import json
import base64
import zipfile
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests

APP_VERSION = "2.0.0"

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MIN_BYTES = 5 * 1024         # reject anything smaller than ~5 KB (broken images)
PAGE_TIMEOUT = 25            # listing-page fetch timeout (seconds)
IMAGE_TIMEOUT = 20           # per-image download timeout (seconds)
DOWNLOAD_WORKERS = 10        # parallel image downloads
DOWNLOAD_ATTEMPTS = 3        # retries per image URL
PAGE_ATTEMPTS = 2            # retries for the listing-page fetch
RETRY_BACKOFF = 0.4          # seconds between retries
NAV_TIMEOUT = 60_000         # browser navigation timeout (ms)
MAX_PER_GROUP = 200          # safety cap per gallery

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PAGE_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

IMAGE_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.propertyfinder.ae/",
}

BAD_WORDS = [
    "logo", "avatar", "agent", "broker", "agency", "profile", "icon",
    "sprite", "placeholder", "default", "badge", "favicon", "tracking", "pixel",
]
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", re.I)
NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def normalize_protocol(url):
    return "https:" + url if url.startswith("//") else url


def slug_from_url(url):
    from urllib.parse import urlparse
    try:
        path = urlparse(url).path
    except ValueError:
        return "propertyfinder-photos"
    segments = [s for s in path.split("/") if s]
    if not segments:
        return "propertyfinder-photos"
    last = re.sub(r"\.\w+$", "", segments[-1]).lower()
    last = re.sub(r"[\s_]+", "-", last)
    last = re.sub(r"[^a-z0-9\-]", "", last)
    last = re.sub(r"-\d+$", "", last)
    last = re.sub(r"-{2,}", "-", last).strip("-")
    return last or "propertyfinder-photos"


def is_image_bytes(content, content_type):
    """Validate that bytes are a real image (not an HTML error page / blank)."""
    if not content or len(content) < MIN_BYTES:
        return False
    if "image" in (content_type or "").lower():
        return True
    if content[:3] == b"\xff\xd8\xff":                       # JPEG
        return True
    if content[:8].startswith(b"\x89PNG\r\n\x1a\n"):         # PNG
        return True
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":  # WEBP
        return True
    if content[:6] in (b"GIF87a", b"GIF89a"):                # GIF
        return True
    return False


def ext_for(content, content_type):
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


# --------------------------------------------------------------------------- #
# Listing data (multiple passes: HTTP retries -> headless browser fallback)
# --------------------------------------------------------------------------- #
def _extract_next_data_json(html):
    if not html:
        return None
    m = NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def next_data_via_requests(url):
    for attempt in range(PAGE_ATTEMPTS):
        try:
            r = requests.get(url, headers=PAGE_HEADERS, timeout=PAGE_TIMEOUT)
        except requests.RequestException:
            time.sleep(RETRY_BACKOFF)
            continue
        if r.status_code == 200:
            data = _extract_next_data_json(r.text)
            if data is not None:
                return data
        time.sleep(RETRY_BACKOFF)
    return None


def next_data_via_playwright(url):
    from playwright.sync_api import sync_playwright

    html = None
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
                user_agent=CHROME_UA, locale="en-US",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            try:
                page.wait_for_selector("#__NEXT_DATA__", timeout=8_000)
            except Exception:
                pass
            html = page.content()
        finally:
            browser.close()
    return _extract_next_data_json(html)


# --------------------------------------------------------------------------- #
# Gallery extraction (hardcoded to PropertyFinder's structure, with a finder
# fallback in case the path shifts)
# --------------------------------------------------------------------------- #
def _image_tasks(arr):
    """
    For each image object return an ordered candidate URL list
    (full first, then medium/original/small as download fallbacks).
    """
    tasks = []
    if not isinstance(arr, list):
        return tasks
    for item in arr[:MAX_PER_GROUP]:
        if not isinstance(item, dict):
            continue
        candidates = []
        for key in ("full", "original", "large", "medium", "url", "small"):
            u = item.get(key)
            if isinstance(u, str) and (u.startswith("http") or u.startswith("//")):
                nu = normalize_protocol(u)
                if nu not in candidates:
                    candidates.append(nu)
        if candidates:
            tasks.append(candidates)
    return tasks


def _find_images_dict(node):
    if isinstance(node, dict):
        for key in ("property", "community"):
            arr = node.get(key)
            if isinstance(arr, list) and arr and isinstance(arr[0], dict) and (
                "full" in arr[0] or "thumbnail" in arr[0]
            ):
                return node
        for v in node.values():
            found = _find_images_dict(v)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_images_dict(item)
            if found:
                return found
    return None


def extract_galleries(data):
    """Return (property_tasks, community_tasks); each is a list of candidate lists."""
    images = None
    try:
        images = data["props"]["pageProps"]["propertyResult"]["property"]["images"]
    except (KeyError, TypeError, IndexError):
        images = None
    if not isinstance(images, dict) or not (images.get("property") or images.get("community")):
        images = _find_images_dict(data)
    if not isinstance(images, dict):
        return [], []
    return _image_tasks(images.get("property")), _image_tasks(images.get("community"))


def generic_fallback_tasks(data):
    """Last resort: scan JSON for image URLs (no property/community split)."""
    out = []
    seen = set()

    def looks_img(s):
        if not isinstance(s, str) or s.startswith("data:"):
            return False
        if not (s.startswith("http") or s.startswith("//")):
            return False
        low = s.lower()
        if ".svg" in low or any(w in low for w in BAD_WORDS):
            return False
        return bool(IMG_EXT_RE.search(s))

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)
        elif looks_img(node):
            u = normalize_protocol(node)
            if u not in seen:
                seen.add(u)
                out.append([u])

    walk(data)
    return out[:MAX_PER_GROUP]


# --------------------------------------------------------------------------- #
# Listing metadata (for the confirmation log line + ZIP manifest)
# --------------------------------------------------------------------------- #
def extract_meta(data):
    try:
        p = data["props"]["pageProps"]["propertyResult"]["property"]
    except (KeyError, TypeError, IndexError):
        return {}
    if not isinstance(p, dict):
        return {}
    meta = {}
    if isinstance(p.get("title"), str):
        meta["title"] = p["title"].strip()
    price = p.get("price")
    if isinstance(price, dict) and price.get("value"):
        try:
            meta["price"] = f"{price.get('currency', '')} {int(price['value']):,}".strip()
        except (TypeError, ValueError):
            pass
    if isinstance(p.get("bedrooms"), (int, float, str)):
        meta["bedrooms"] = p["bedrooms"]
    if isinstance(p.get("bathrooms"), (int, float, str)):
        meta["bathrooms"] = p["bathrooms"]
    size = p.get("size")
    if isinstance(size, dict) and size.get("value"):
        meta["size"] = f"{size['value']} {size.get('unit', '')}".strip()
    loc = p.get("location")
    if isinstance(loc, dict) and loc.get("full_name"):
        meta["location"] = loc["full_name"]
    for k in ("property_type", "offering_type", "reference"):
        if isinstance(p.get(k), str):
            meta[k] = p[k]
    return meta


def build_manifest(url, meta, n_prop, n_comm, n_other):
    lines = ["PropertyFinder listing photos", "=" * 32, ""]

    def add(label, val):
        if val not in (None, ""):
            lines.append(f"{label:<12}{val}")

    add("Title:", meta.get("title"))
    add("Reference:", meta.get("reference"))
    add("Price:", meta.get("price"))
    ptype = meta.get("property_type")
    if ptype and meta.get("offering_type"):
        ptype = f"{ptype} ({meta['offering_type']})"
    add("Type:", ptype)
    if meta.get("bedrooms") is not None or meta.get("bathrooms") is not None:
        add("Beds/Baths:", f"{meta.get('bedrooms', '?')} / {meta.get('bathrooms', '?')}")
    add("Size:", meta.get("size"))
    add("Location:", meta.get("location"))
    add("Source:", url)
    lines.append("")
    if n_other:
        lines.append(f"Photos: {n_other}")
    else:
        lines.append(f"Property photos:   {n_prop}   (property/)")
        lines.append(f"Community photos:  {n_comm}   (community/)")
        lines.append(f"Total:             {n_prop + n_comm}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Downloading (multiple passes per image) + ZIP packaging
# --------------------------------------------------------------------------- #
def download_one(candidates):
    """Try each candidate URL with retries; return (content, ctype) or None."""
    for url in candidates:
        for attempt in range(DOWNLOAD_ATTEMPTS):
            try:
                r = requests.get(url, headers=IMAGE_HEADERS, timeout=IMAGE_TIMEOUT)
            except requests.RequestException:
                time.sleep(RETRY_BACKOFF)
                continue
            ctype = r.headers.get("Content-Type", "")
            if r.status_code == 200 and is_image_bytes(r.content, ctype):
                return r.content, ctype
            if r.status_code in (400, 401, 403, 404, 410):
                break  # permanent for this URL; move to next candidate
            time.sleep(RETRY_BACKOFF)
    return None


def build_zip(groups, info_text=None):
    """groups: list of (folder_name, [(content, ctype), ...])."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if info_text:
            zf.writestr("_info.txt", info_text)
        for folder, photos in groups:
            for index, (content, ctype) in enumerate(photos, start=1):
                name = f"photo_{index:02d}.{ext_for(content, ctype)}"
                zf.writestr(f"{folder}/{name}" if folder else name, content)
    buffer.seek(0)
    return buffer


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _health_payload():
    browser = False
    try:
        import playwright  # noqa: F401
        browser = True
    except Exception:
        browser = False
    return {
        "status": "ok",
        "service": "propertyfinder-photo-downloader",
        "version": APP_VERSION,
        "browser_fallback": browser,
    }


@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify(_health_payload())


@app.route("/scrape", methods=["POST", "OPTIONS"])
def scrape():
    if request.method == "OPTIONS":
        return ("", 204)

    data_in = request.get_json(silent=True) or {}
    url = (data_in.get("url") or "").strip()
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
            yield nd({"type": "log", "message": "Reading listing data"})
            data = next_data_via_requests(url)
            if data is None:
                yield nd({"type": "log",
                          "message": "Direct read blocked - opening in headless browser"})
                try:
                    data = next_data_via_playwright(url)
                except Exception:
                    data = None
            if data is None:
                yield nd({"type": "error",
                          "message": "Could not read this listing. It may be slow or "
                                     "blocking automated access - try again."})
                return

            meta = extract_meta(data)
            if meta.get("title"):
                yield nd({"type": "log", "message": "Listing: " + meta["title"]})
            bits = []
            if meta.get("price"):
                bits.append(meta["price"])
            if meta.get("bedrooms") is not None:
                bits.append(str(meta["bedrooms"]) + " bed")
            if meta.get("bathrooms") is not None:
                bits.append(str(meta["bathrooms"]) + " bath")
            if meta.get("size"):
                bits.append(meta["size"])
            if meta.get("location"):
                bits.append(meta["location"])
            if bits:
                yield nd({"type": "log", "message": " · ".join(bits)})

            prop_tasks, comm_tasks = extract_galleries(data)
            split = bool(prop_tasks or comm_tasks)
            if split:
                tasks = ([("property", c) for c in prop_tasks] +
                         [("community", c) for c in comm_tasks])
                yield nd({"type": "log", "message": f"Property images: {len(prop_tasks)}"})
                yield nd({"type": "log", "message": f"Community images: {len(comm_tasks)}"})
                yield nd({"type": "log",
                          "message": f"Total: {len(prop_tasks) + len(comm_tasks)} images"})
            else:
                fb = generic_fallback_tasks(data)
                tasks = [("", c) for c in fb]
                yield nd({"type": "log",
                          "message": f"Found {len(fb)} images (no property/community split)"})

            if not tasks:
                yield nd({"type": "error", "message": "No photos found on this listing."})
                return

            total = len(tasks)
            results = [None] * total
            done_n = 0
            yield nd({"type": "progress", "done": 0, "total": total,
                      "message": f"Downloading photos 0/{total}"})
            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                fut_to_index = {pool.submit(download_one, cands): i
                                for i, (_tag, cands) in enumerate(tasks)}
                for fut in as_completed(fut_to_index):
                    i = fut_to_index[fut]
                    try:
                        results[i] = fut.result()
                    except Exception:
                        results[i] = None
                    done_n += 1
                    yield nd({"type": "progress", "done": done_n, "total": total,
                              "message": f"Downloading photos {done_n}/{total}"})

            prop_photos, comm_photos, other_photos = [], [], []
            seen = set()
            for (tag, _cands), res in zip(tasks, results):
                if not res:
                    continue
                content, ctype = res
                digest = hashlib.sha256(content).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                if tag == "property":
                    prop_photos.append((content, ctype))
                elif tag == "community":
                    comm_photos.append((content, ctype))
                else:
                    other_photos.append((content, ctype))

            total_photos = len(prop_photos) + len(comm_photos) + len(other_photos)
            failed = total - sum(1 for r in results if r)
            if total_photos == 0:
                yield nd({"type": "error",
                          "message": "No photos could be downloaded from this listing."})
                return
            if failed:
                yield nd({"type": "log",
                          "message": f"{failed} image(s) could not be downloaded after retries"})

            if other_photos:
                groups = [("", other_photos)]
                yield nd({"type": "log", "message": f"Packaging ZIP ({total_photos} photos)"})
            else:
                groups = [(f, p) for f, p in
                          (("property", prop_photos), ("community", comm_photos)) if p]
                yield nd({"type": "log",
                          "message": f"Packaging ZIP ({len(prop_photos)} property, "
                                     f"{len(comm_photos)} community)"})

            manifest = build_manifest(url, meta, len(prop_photos),
                                      len(comm_photos), len(other_photos))
            zip_buffer = build_zip(groups, info_text=manifest)
            filename = f"{slug_from_url(url)}.zip"
            encoded = base64.b64encode(zip_buffer.getvalue()).decode("ascii")
            yield nd({
                "type": "done",
                "count": total_photos,
                "property": len(prop_photos),
                "community": len(comm_photos),
                "failed": failed,
                "title": meta.get("title"),
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
