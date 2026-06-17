"""
PropertyFinder Photo ZIP Downloader - backend

Reads a PropertyFinder listing's __NEXT_DATA__ (server-rendered JSON) and pulls
the FULL-resolution gallery photos, split into the same tabs the site shows:

    property[]   -> the listing's own photos      -> property/  in the ZIP
    community[]  -> community / area photos        -> community/ in the ZIP

Primary path is a plain HTTP request (fast); a headless-Chromium fallback covers
the case where the page is served behind a bot check.

POST /scrape streams newline-delimited JSON (NDJSON) progress; the final "done"
message carries the ZIP (base64) plus the property / community / total counts.
"""

import io
import os
import re
import json
import base64
import zipfile
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MIN_BYTES = 6 * 1024         # reject anything smaller than ~6 KB (broken images)
REQUEST_TIMEOUT = 25         # per-request timeout (seconds)
DOWNLOAD_WORKERS = 10        # parallel image downloads
NAV_TIMEOUT = 60_000         # browser navigation timeout (ms)
MAX_PER_GROUP = 150          # safety cap per gallery

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Headers for fetching the listing HTML (mimic a real browser navigation).
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

# Headers for downloading the images themselves.
IMAGE_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.propertyfinder.ae/",
}

# Fallback-only: words that mark an image as NOT a listing photo.
BAD_WORDS = [
    "logo", "avatar", "agent", "broker", "agency", "profile", "icon",
    "sprite", "placeholder", "default", "badge", "watermark-logo",
    "svg", "favicon", "tracking", "pixel",
]
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", re.I)
NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_protocol(url):
    return "https:" + url if url.startswith("//") else url


def slug_from_url(url):
    """Build a filename slug from the listing URL path."""
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
    """Fast path: plain HTTP GET of the listing page."""
    try:
        r = requests.get(url, headers=PAGE_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return _extract_next_data_json(r.text)


def next_data_via_playwright(url):
    """Fallback path: load the page in headless Chromium (gets past bot checks)."""
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


def _fulls(arr):
    """From a list of image objects, return the full-resolution URLs in order."""
    out = []
    if isinstance(arr, list):
        for item in arr[:MAX_PER_GROUP]:
            if isinstance(item, dict):
                u = item.get("full") or item.get("original") or item.get("medium") or item.get("url")
                if isinstance(u, str) and (u.startswith("http") or u.startswith("//")):
                    out.append(normalize_protocol(u))
    return out


def _find_images_dict(node):
    """Recursively locate the images object holding property/community arrays."""
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
    """Return (property_full_urls, community_full_urls) from __NEXT_DATA__."""
    images = None
    try:
        images = data["props"]["pageProps"]["propertyResult"]["property"]["images"]
    except (KeyError, TypeError, IndexError):
        images = None
    if not isinstance(images, dict) or not (images.get("property") or images.get("community")):
        images = _find_images_dict(data)

    prop = _fulls(images.get("property")) if isinstance(images, dict) else []
    comm = _fulls(images.get("community")) if isinstance(images, dict) else []
    return prop, comm


def _generic_fallback_images(data):
    """Last resort: scan the JSON for image URLs (no property/community split)."""
    found = []
    seen = set()

    def looks_img(s):
        if not isinstance(s, str) or s.startswith("data:"):
            return False
        if not (s.startswith("http") or s.startswith("//")):
            return False
        low = s.lower()
        if ".svg" in low:
            return False
        if any(w in low for w in BAD_WORDS):
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
                found.append(u)

    walk(data)
    return found[:MAX_PER_GROUP]


def _ext_for(content, content_type):
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


def download_one(url):
    try:
        r = requests.get(url, headers=IMAGE_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if r.status_code == 200 and r.content and len(r.content) >= MIN_BYTES:
        return r.content, r.headers.get("Content-Type", "")
    return None


def build_zip(groups):
    """groups: list of (folder_name, [(content, ctype), ...])."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder, photos in groups:
            for index, (content, ctype) in enumerate(photos, start=1):
                name = f"photo_{index:02d}.{_ext_for(content, ctype)}"
                path = f"{folder}/{name}" if folder else name
                zf.writestr(path, content)
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

            prop_urls, comm_urls = extract_galleries(data)
            tasks = ([("property", u) for u in prop_urls] +
                     [("community", u) for u in comm_urls])

            if tasks:
                yield nd({"type": "log", "message": f"Property images: {len(prop_urls)}"})
                yield nd({"type": "log", "message": f"Community images: {len(comm_urls)}"})
                yield nd({"type": "log",
                          "message": f"Total: {len(prop_urls) + len(comm_urls)} images"})
            else:
                other = _generic_fallback_images(data)
                tasks = [("", u) for u in other]
                yield nd({"type": "log",
                          "message": f"Found {len(other)} images (no property/community split)"})

            if not tasks:
                yield nd({"type": "error", "message": "No photos found on this listing."})
                return

            total = len(tasks)
            results = [None] * total
            done_n = 0
            yield nd({"type": "progress", "done": 0, "total": total,
                      "message": f"Downloading photos 0/{total}"})
            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
                fut_to_index = {pool.submit(download_one, url_): i
                                for i, (_tag, url_) in enumerate(tasks)}
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
            for (tag, _url), res in zip(tasks, results):
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
            if total_photos == 0:
                yield nd({"type": "error",
                          "message": "No photos could be downloaded from this listing."})
                return

            if other_photos:
                groups = [("", other_photos)]
            else:
                groups = [("property", prop_photos), ("community", comm_photos)]
                groups = [(f, p) for f, p in groups if p]

            yield nd({"type": "log",
                      "message": f"Packaging ZIP ({len(prop_photos)} property, "
                                 f"{len(comm_photos)} community)"
                                 if not other_photos else
                                 f"Packaging ZIP ({total_photos} photos)"})

            zip_buffer = build_zip(groups)
            filename = f"{slug_from_url(url)}.zip"
            encoded = base64.b64encode(zip_buffer.getvalue()).decode("ascii")
            yield nd({
                "type": "done",
                "count": total_photos,
                "property": len(prop_photos),
                "community": len(comm_photos),
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
