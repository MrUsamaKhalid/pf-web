"""
PropertyFinder Photo ZIP Downloader — backend v3

Reads a PropertyFinder listing's __NEXT_DATA__ (server-rendered JSON) and pulls
the full-resolution gallery photos, split into the same tabs the site shows:

    images.property[] (+ images.tower[])  ->  property/   in the ZIP
    images.community[]                    ->  community/  in the ZIP

Transport (v3): POST /scrape streams NDJSON progress and the final "done" frame
carries a DOWNLOAD TOKEN, not the file. GET /zip/<token> then streams the real
archive with Content-Disposition. Images are written to disk as they arrive and
the archive is assembled from disk, so peak memory is roughly one chunk instead
of several copies of the whole ZIP.

Endpoints
---------
GET  /              health
GET  /health        health
GET  /capabilities  option spec + limits (the frontend renders controls from this)
POST /scrape        NDJSON stream: log | meta | notice | progress | error | done
GET  /zip/<token>   the assembled ZIP (single use, 10 minute TTL)
"""

import os
import re
import time
import json
import uuid
import shutil
import zipfile
import hashlib
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import requests

APP_VERSION = "3.0.0"

app = Flask(__name__)
# Render terminates TLS in front of us; trust one hop so rate limiting sees the
# real client instead of the proxy.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "https://mrusamakhalid.github.io"
).split(",") if o.strip()]
CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}})

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MIN_BYTES = 5 * 1024
PAGE_TIMEOUT = 20
IMAGE_TIMEOUT = 12
DOWNLOAD_WORKERS = 10
DOWNLOAD_ATTEMPTS = 2
PAGE_ATTEMPTS = 2
RETRY_BACKOFF = 0.4
NAV_TIMEOUT = 45_000
MAX_PER_GROUP = 150
MAX_TOTAL_BYTES = 100 * 1024 * 1024   # archives stream to disk, so this is a
                                      # bandwidth/disk guard, not a memory one
JOB_DEADLINE = 140                    # self-abort before gunicorn's timeout
ZIP_TTL = 600                         # download token lifetime (seconds)

MAX_CONCURRENT_JOBS = 2
RATE_CAPACITY = 12                    # jobs per IP...
RATE_REFILL_PER_SEC = 12 / 3600.0     # ...refilling over an hour

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

IMAGE_HEADERS_BASE = {
    "User-Agent": CHROME_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.propertyfinder.ae/",
}
# The CDN picks the format purely from Accept: omit image/webp and it returns
# JPEG. That makes "give me JPEG" free — no Pillow, no re-encode, no CPU.
ACCEPT_JPEG = "image/jpeg,image/png;q=0.9,*/*;q=0.8"
ACCEPT_ANY = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"

BAD_WORDS = [
    "logo", "avatar", "agent", "broker", "agency", "profile", "icon",
    "sprite", "placeholder", "default", "badge", "favicon", "tracking", "pixel",
]
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:[?#]|$)", re.I)
NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
# Both the listing page and every image URL must live on a PropertyFinder host.
HOST_RE = re.compile(r"(?:[a-z0-9-]+\.)*propertyfinder\.(?:ae|com|qa|bh|sa|eg|lb)")

# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #
OPTION_SPEC = {
    "property":    {"type": "bool", "default": True, "label": "Property photos"},
    "community":   {"type": "bool", "default": True, "label": "Community photos"},
    "info":        {"type": "bool", "default": True, "label": "Include info file"},
    "info_format": {"type": "enum", "default": "txt", "values": ["txt", "json"],
                    "label": "Info file format"},
    "format":      {"type": "enum", "default": "jpeg", "values": ["jpeg", "original"],
                    "label": "Property photo format",
                    "note": "Community photos are always JPEG."},
    "structure":   {"type": "enum", "default": "grouped",
                    "values": ["grouped", "flat", "by_listing"],
                    "label": "Folder structure"},
    "max_images":  {"type": "int", "default": 80, "min": 1, "max": MAX_PER_GROUP,
                    "label": "Max photos per gallery"},
    # Present in the schema, deliberately not offered in the UI: the agency
    # watermark is burned into the master PropertyFinder stores, so there is no
    # honest "remove" to implement.
    "watermark":   {"type": "enum", "default": "off", "values": ["off"],
                    "label": "Watermark"},
}
DEFAULTS = {k: v["default"] for k, v in OPTION_SPEC.items()}


def normalize_options(raw):
    """Coerce a client payload into a valid option set.

    Returns (opts, notices, error). Never raises. Unknown keys are dropped, bad
    values fall back to the default and surface a notice, and only genuinely
    unrunnable combinations produce an error.
    """
    opts = dict(DEFAULTS)
    notices = []
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return opts, ["Options were ignored (expected an object)."], None

    for key, value in raw.items():
        spec = OPTION_SPEC.get(key)
        if spec is None:
            app.logger.info("dropping unknown option %r", key)
            continue
        if spec["type"] == "bool":
            if isinstance(value, bool):
                opts[key] = value
            else:
                notices.append(f"{spec['label']}: expected true/false, using default.")
        elif spec["type"] == "enum":
            if isinstance(value, str) and value in spec["values"]:
                opts[key] = value
            else:
                notices.append(
                    f"{spec['label']}: '{value}' isn't supported, using '{spec['default']}'.")
        elif spec["type"] == "int":
            try:
                n = int(value)
            except (TypeError, ValueError):
                notices.append(f"{spec['label']}: expected a number, using default.")
                continue
            opts[key] = max(spec["min"], min(spec["max"], n))

    if not opts["property"] and not opts["community"]:
        return opts, notices, "Select at least one gallery — property or community."
    return opts, notices, None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def normalize_protocol(url):
    return "https:" + url if url.startswith("//") else url


def host_ok(url):
    try:
        return bool(HOST_RE.fullmatch((urlparse(url).hostname or "").lower()))
    except ValueError:
        return False


def slug_from_url(url):
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


def is_image_head(head, content_type):
    if "image" in (content_type or "").lower():
        return True
    if head[:3] == b"\xff\xd8\xff":
        return True
    if head[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if head[:4] == b"RIFF":                       # WEBP (RIFF....WEBP)
        return True
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return False


def ext_for(head, content_type):
    c = (content_type or "").lower()
    if "png" in c:
        return "png"
    if "webp" in c:
        return "webp"
    if "jpeg" in c or "jpg" in c:
        return "jpg"
    if head[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF":
        return "webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "jpg"


# --------------------------------------------------------------------------- #
# Listing data
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
    for _ in range(PAGE_ATTEMPTS):
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


_BROWSER_LOCK = threading.Semaphore(1)


def next_data_via_playwright(url):
    from playwright.sync_api import sync_playwright

    html = None
    with _BROWSER_LOCK:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            try:
                ctx = browser.new_context(user_agent=CHROME_UA, locale="en-US",
                                          viewport={"width": 1366, "height": 900})
                page = ctx.new_page()
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
# Galleries + metadata
# --------------------------------------------------------------------------- #
def _image_tasks(arr, limit):
    tasks = []
    if not isinstance(arr, list) or limit <= 0:
        return tasks
    for item in arr[:limit]:
        if not isinstance(item, dict):
            continue
        candidates = []
        for key in ("full", "original", "large", "medium", "url", "small"):
            u = item.get(key)
            if isinstance(u, str) and (u.startswith("http") or u.startswith("//")):
                nu = normalize_protocol(u)
                if nu not in candidates and host_ok(nu):
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


def looks_like_listing(data):
    """True only for a property detail page.

    A removed listing redirects to search, whose __NEXT_DATA__ is full of
    result-card thumbnails and other people's property metadata. Without this
    check the generic fallback happily packages 79 search thumbnails and labels
    them with the wrong listing.
    """
    try:
        pp = data["props"]["pageProps"]
    except (KeyError, TypeError):
        return False
    if not isinstance(pp, dict):
        return False
    if isinstance(pp.get("propertyResult"), dict):
        return True
    page = str(data.get("page") or "")
    name = str(pp.get("pageName") or "")
    if page.startswith("/search") or "search" in name or "filtersData" in pp:
        return False
    return _find_images_dict(data) is not None


def extract_galleries(data, limit):
    """Return (property_tasks, community_tasks, structured). tower folds into property."""
    images = None
    try:
        images = data["props"]["pageProps"]["propertyResult"]["property"]["images"]
    except (KeyError, TypeError, IndexError):
        images = None
    if not isinstance(images, dict) or not (images.get("property") or images.get("community")):
        images = _find_images_dict(data)
    if not isinstance(images, dict):
        return [], [], False
    prop = _image_tasks(images.get("property"), limit)
    prop += _image_tasks(images.get("tower"), limit - len(prop))
    comm = _image_tasks(images.get("community"), limit)
    return prop, comm, True


def generic_fallback_tasks(data, limit):
    out, seen = [], set()

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
            if u not in seen and host_ok(u):
                seen.add(u)
                out.append([u])

    walk(data)
    return out[:limit]


def _find_property_node(node):
    if isinstance(node, dict):
        if isinstance(node.get("title"), str) and ("price" in node or "bedrooms" in node):
            return node
        for v in node.values():
            found = _find_property_node(v)
            if found:
                return found
    elif isinstance(node, list):
        for it in node:
            found = _find_property_node(it)
            if found:
                return found
    return None


def extract_meta(data):
    """Listing metadata, with a structural fallback if the known path shifts."""
    p = None
    try:
        p = data["props"]["pageProps"]["propertyResult"]["property"]
    except (KeyError, TypeError, IndexError):
        p = None
    if not isinstance(p, dict) or "title" not in p:
        p = _find_property_node(data)
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
    for key in ("bedrooms", "bathrooms"):
        if isinstance(p.get(key), (int, float, str)):
            meta[key] = p[key]
    size = p.get("size")
    if isinstance(size, dict) and size.get("value"):
        meta["size"] = f"{size['value']} {size.get('unit', '')}".strip()
    loc = p.get("location")
    if isinstance(loc, dict) and loc.get("full_name"):
        meta["location"] = loc["full_name"]
    for k in ("property_type", "offering_type", "reference"):
        if isinstance(p.get(k), str):
            meta[k] = p[k]
    for k in ("listing_id", "id"):
        if p.get(k) is not None and "listing_id" not in meta:
            meta["listing_id"] = str(p[k])
    # the agent/brokerage the listing already carries — no need to ask the user
    agent = p.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("name"), str):
        meta["agent"] = agent["name"].strip()
    broker = p.get("broker")
    if isinstance(broker, dict) and isinstance(broker.get("name"), str):
        meta["broker"] = broker["name"].strip()
    return meta


def build_manifest_txt(url, meta, n_prop, n_comm, n_other, opts):
    lines = ["PropertyFinder listing photos", "=" * 32, ""]

    def add(label, val):
        if val not in (None, ""):
            lines.append(f"{label:<12}{val}")

    add("Title:", meta.get("title"))
    add("Reference:", meta.get("reference"))
    add("Listing ID:", meta.get("listing_id"))
    add("Price:", meta.get("price"))
    ptype = meta.get("property_type")
    if ptype and meta.get("offering_type"):
        ptype = f"{ptype} ({meta['offering_type']})"
    add("Type:", ptype)
    if meta.get("bedrooms") is not None or meta.get("bathrooms") is not None:
        add("Beds/Baths:", f"{meta.get('bedrooms', '?')} / {meta.get('bathrooms', '?')}")
    add("Size:", meta.get("size"))
    add("Location:", meta.get("location"))
    add("Agent:", meta.get("agent"))
    add("Brokerage:", meta.get("broker"))
    add("Source:", url)
    add("Format:", "JPEG" if opts["format"] == "jpeg" else "original (WebP where served)")
    lines.append("")
    if n_other:
        lines.append(f"Photos: {n_other}")
    else:
        lines.append(f"Property photos:   {n_prop}")
        lines.append(f"Community photos:  {n_comm}")
        lines.append(f"Total:             {n_prop + n_comm}")
    return "\n".join(lines) + "\n"


def build_manifest_json(url, meta, entries, opts):
    return json.dumps({
        "source": url,
        "listing": meta,
        "options": opts,
        "photos": entries,
        "counts": {
            "property": sum(1 for e in entries if e["group"] == "property"),
            "community": sum(1 for e in entries if e["group"] == "community"),
            "total": len(entries),
        },
    }, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# Downloading (streams to disk — peak memory is one chunk, not one ZIP)
# --------------------------------------------------------------------------- #
def image_headers(want_jpeg):
    h = dict(IMAGE_HEADERS_BASE)
    h["Accept"] = ACCEPT_JPEG if want_jpeg else ACCEPT_ANY
    return h


def download_to(dest_path, candidates, want_jpeg):
    headers = image_headers(want_jpeg)
    for url in candidates:
        for _ in range(DOWNLOAD_ATTEMPTS):
            try:
                r = requests.get(url, headers=headers, timeout=IMAGE_TIMEOUT, stream=True)
            except requests.RequestException:
                time.sleep(RETRY_BACKOFF)
                continue
            ok = False
            ctype = ""
            digest = hashlib.sha256()
            size = 0
            head = b""
            try:
                # a redirect must not walk us off PropertyFinder's hosts
                if not host_ok(r.url):
                    break
                if r.status_code != 200:
                    if r.status_code in (400, 401, 403, 404, 410):
                        break
                    time.sleep(RETRY_BACKOFF)
                    continue
                ctype = r.headers.get("Content-Type", "")
                with open(dest_path, "wb") as fh:
                    for chunk in r.iter_content(64 * 1024):
                        if not chunk:
                            continue
                        if len(head) < 16:
                            head += chunk[:16 - len(head)]
                        fh.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                ok = True
            except Exception:
                ok = False
            finally:
                r.close()

            if ok and size >= MIN_BYTES and is_image_head(head, ctype):
                return {"ctype": ctype, "size": size, "sha": digest.hexdigest(),
                        "ext": ext_for(head, ctype), "url": url}
            try:
                os.remove(dest_path)
            except OSError:
                pass
    return None


def folder_for(group, opts, slug):
    if opts["structure"] == "flat":
        return ""
    if opts["structure"] == "by_listing":
        return f"{slug}/{group}" if group else slug
    return group


# --------------------------------------------------------------------------- #
# Download-token store
# --------------------------------------------------------------------------- #
_ZIPS = {}
_ZIPS_LOCK = threading.Lock()


def sweep_zips():
    now = time.time()
    with _ZIPS_LOCK:
        dead = [t for t, v in _ZIPS.items() if v["expires"] < now]
        for t in dead:
            entry = _ZIPS.pop(t, None)
            if entry:
                try:
                    os.remove(entry["path"])
                except OSError:
                    pass


def register_zip(path, filename):
    token = uuid.uuid4().hex
    with _ZIPS_LOCK:
        _ZIPS[token] = {"path": path, "filename": filename,
                        "expires": time.time() + ZIP_TTL}
    return token


# --------------------------------------------------------------------------- #
# Throttling — the replacement for the login that used to gate this endpoint
# --------------------------------------------------------------------------- #
_JOB_SLOTS = threading.Semaphore(MAX_CONCURRENT_JOBS)
_RATE = {}
_RATE_LOCK = threading.Lock()


def rate_allow(ip):
    now = time.time()
    with _RATE_LOCK:
        tokens, last = _RATE.get(ip, (float(RATE_CAPACITY), now))
        tokens = min(RATE_CAPACITY, tokens + (now - last) * RATE_REFILL_PER_SEC)
        if tokens < 1:
            _RATE[ip] = (tokens, now)
            return False
        _RATE[ip] = (tokens - 1, now)
        if len(_RATE) > 4096:
            for k in [k for k, v in list(_RATE.items()) if now - v[1] > 7200]:
                _RATE.pop(k, None)
        return True


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _health_payload():
    try:
        import playwright  # noqa: F401
        browser = True
    except Exception:
        browser = False
    return {"status": "ok", "service": "propertyfinder-photo-downloader",
            "version": APP_VERSION, "browser_fallback": browser}


@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify(_health_payload())


@app.route("/capabilities", methods=["GET"])
def capabilities():
    return jsonify({
        "version": APP_VERSION,
        "options": OPTION_SPEC,
        "defaults": DEFAULTS,
        "job_deadline": JOB_DEADLINE,
        "zip_ttl": ZIP_TTL,
        "max_total_bytes": MAX_TOTAL_BYTES,
    })


@app.route("/zip/<token>", methods=["GET"])
def get_zip(token):
    sweep_zips()
    with _ZIPS_LOCK:
        entry = _ZIPS.get(token)
    if not entry or not os.path.exists(entry["path"]):
        return jsonify({"error": "This download link has expired — run it again.",
                        "code": "expired"}), 404

    path, filename = entry["path"], entry["filename"]
    size = os.path.getsize(path)

    def stream():
        # Deliberately re-usable until the TTL expires: a row can be downloaded
        # again, and batch can read the same archive twice (once per item, once
        # for a combined ZIP). The janitor sweep is what reclaims the disk.
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                yield chunk

    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "-", filename) or "photos.zip"
    return Response(stream(), mimetype="application/zip", headers={
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename)}"
        ),
        "Content-Length": str(size),
        "Cache-Control": "no-store",
    })


@app.route("/scrape", methods=["POST", "OPTIONS"])
def scrape():
    if request.method == "OPTIONS":
        return ("", 204)

    sweep_zips()
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing 'url' in request body.", "code": "bad_request"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if not host_ok(url):
        return jsonify({"error": "URL must be a PropertyFinder listing.", "code": "bad_url"}), 400

    # validated here, in the view body — inside generate() the 200 is already
    # committed and a validation failure would be indistinguishable from a
    # scrape failure.
    opts, notices, opt_error = normalize_options(payload.get("options"))
    if opt_error:
        return jsonify({"error": opt_error, "code": "bad_option"}), 400

    client_ip = request.headers.get(
        "X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    if not rate_allow(client_ip):
        return jsonify({"error": "Too many downloads from this connection. Try again later.",
                        "code": "rate_limited"}), 429, {"Retry-After": "300"}
    if not _JOB_SLOTS.acquire(blocking=False):
        return jsonify({"error": "The server is busy with another download. Try again in a moment.",
                        "code": "busy"}), 429, {"Retry-After": "30"}

    def nd(obj):
        return json.dumps(obj) + "\n"

    def generate():
        started = time.monotonic()
        workdir = tempfile.mkdtemp(prefix="pfjob-")
        zip_path = None
        try:
            for note in notices:
                yield nd({"type": "notice", "message": note})

            yield nd({"type": "log", "message": "Reading listing data"})
            data = next_data_via_requests(url)
            if data is None:
                app.logger.info("playwright fallback engaged for %s", url)
                yield nd({"type": "log", "message": "Direct read blocked — opening a browser"})
                try:
                    data = next_data_via_playwright(url)
                except Exception:
                    app.logger.exception("playwright fallback failed")
                    data = None
            if data is None:
                yield nd({"type": "error", "code": "listing_unreadable",
                          "message": "Could not read this listing. It may be slow or "
                                     "blocking automated access — try again."})
                return

            if not looks_like_listing(data):
                yield nd({"type": "error", "code": "not_a_listing",
                          "message": "That link doesn't open a listing — it was probably "
                                     "removed or renamed. Open it in a browser and copy "
                                     "the URL again."})
                return

            meta = extract_meta(data)
            slug = slug_from_url(url)
            if meta.get("title"):
                yield nd({"type": "log", "message": "Listing: " + meta["title"]})
            bits = []
            for key, suffix in (("price", ""), ("bedrooms", " bed"), ("bathrooms", " bath"),
                                ("size", ""), ("location", "")):
                if meta.get(key) is not None:
                    bits.append(f"{meta[key]}{suffix}")
            if bits:
                yield nd({"type": "log", "message": " · ".join(str(b) for b in bits)})
            yield nd({"type": "meta", "listing": meta, "slug": slug,
                      "suggested_filename": (slug or "propertyfinder-photos") + ".zip"})

            limit = int(opts["max_images"])
            prop_tasks, comm_tasks, structured = extract_galleries(data, limit)
            if not structured or (not prop_tasks and not comm_tasks):
                fb = generic_fallback_tasks(data, limit)
                if fb:
                    yield nd({"type": "notice",
                              "message": "The property/community split wasn't available for "
                                         "this listing — downloading everything found."})
                tasks = [("", c) for c in fb]
                yield nd({"type": "log", "message": f"Found {len(fb)} images"})
            else:
                tasks = []
                if opts["property"]:
                    tasks += [("property", c) for c in prop_tasks]
                if opts["community"]:
                    tasks += [("community", c) for c in comm_tasks]
                yield nd({"type": "log",
                          "message": f"Property images: {len(prop_tasks)}"
                                     + ("" if opts["property"] else " (skipped)")})
                yield nd({"type": "log",
                          "message": f"Community images: {len(comm_tasks)}"
                                     + ("" if opts["community"] else " (skipped)")})
                yield nd({"type": "log", "message": f"Downloading {len(tasks)} images"})

            if not tasks:
                msg = ("Everything on this listing was excluded by your options."
                       if (prop_tasks or comm_tasks) else "No photos found on this listing.")
                yield nd({"type": "error", "code": "no_photos", "message": msg})
                return

            total = len(tasks)
            want_jpeg = opts["format"] == "jpeg"
            results = [None] * total
            done_n = 0
            timed_out = False

            yield nd({"type": "progress", "done": 0, "total": total})
            pool = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS)
            try:
                futures = {
                    pool.submit(download_to, os.path.join(workdir, f"{i:04d}.bin"),
                                cands, want_jpeg): i
                    for i, (_tag, cands) in enumerate(tasks)
                }
                for fut in as_completed(futures):
                    i = futures[fut]
                    try:
                        results[i] = fut.result()
                    except Exception:
                        results[i] = None
                    done_n += 1
                    yield nd({"type": "progress", "done": done_n, "total": total})
                    if time.monotonic() - started > JOB_DEADLINE:
                        timed_out = True
                        break
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

            # assemble: walk in gallery order, dedupe by content hash, stream from disk
            entries = []
            seen = set()
            total_bytes = 0
            capped = False
            counters = {"property": 0, "community": 0, "": 0}
            for i, ((tag, _c), info) in enumerate(zip(tasks, results)):
                if not info or info["sha"] in seen:
                    continue
                if total_bytes + info["size"] > MAX_TOTAL_BYTES:
                    capped = True
                    continue
                seen.add(info["sha"])
                total_bytes += info["size"]
                counters[tag] = counters.get(tag, 0) + 1
                entries.append({
                    "src": os.path.join(workdir, f"{i:04d}.bin"),
                    "group": tag, "index": counters[tag], "ext": info["ext"],
                    "bytes": info["size"], "sha256": info["sha"], "url": info["url"],
                })

            downloaded = sum(1 for r in results if r)
            failed = total - downloaded
            if not entries:
                yield nd({"type": "error", "code": "download_failed",
                          "message": "No photos could be downloaded from this listing."})
                return
            if failed:
                yield nd({"type": "log",
                          "message": f"{failed} image(s) could not be downloaded after retries"})
            if capped:
                yield nd({"type": "notice",
                          "message": "Size limit reached — some photos were left out."})
            if timed_out:
                yield nd({"type": "notice",
                          "message": "Time limit reached — packaging what finished."})

            n_prop = counters.get("property", 0)
            n_comm = counters.get("community", 0)
            n_other = counters.get("", 0)
            yield nd({"type": "log",
                      "message": (f"Packaging ZIP ({n_prop} property, {n_comm} community)"
                                  if not n_other else f"Packaging ZIP ({n_other} photos)")})

            pad = max(2, len(str(max(counters.values()))))
            fd, zip_path = tempfile.mkstemp(prefix="pfzip-", suffix=".zip")
            os.close(fd)
            manifest_entries = []
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for e in entries:
                    group = e["group"]
                    folder = folder_for(group, opts, slug)
                    stem = f"{group}-" if (opts["structure"] == "flat" and group) else ""
                    name = f"{stem}{e['index']:0{pad}d}.{e['ext']}"
                    arc = f"{folder}/{name}" if folder else name
                    zf.write(e["src"], arc)
                    manifest_entries.append({"file": arc, "group": group or "photos",
                                             "bytes": e["bytes"], "sha256": e["sha256"],
                                             "source": e["url"]})
                if opts["info"]:
                    if opts["info_format"] == "json":
                        zf.writestr("_info.json",
                                    build_manifest_json(url, meta, manifest_entries, opts))
                    else:
                        zf.writestr("_info.txt",
                                    build_manifest_txt(url, meta, n_prop, n_comm, n_other, opts))

            shutil.rmtree(workdir, ignore_errors=True)
            filename = f"{slug}.zip"
            token = register_zip(zip_path, filename)
            zip_path = None  # ownership handed to the token store

            yield nd({
                "type": "done",
                "download": f"/zip/{token}",
                "filename": filename,
                "count": len(entries),
                "property": n_prop,
                "community": n_comm,
                "failed": failed,
                "truncated": bool(timed_out or capped),
                "bytes": total_bytes,
                "title": meta.get("title"),
                "expires_in": ZIP_TTL,
            })
        except Exception:
            app.logger.exception("scrape failed for %s", url)
            yield nd({"type": "error", "code": "server_error",
                      "message": "Something went wrong while building the ZIP."})
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if zip_path:
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
            _JOB_SLOTS.release()

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Not found.", "code": "not_found"}), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"error": "Method not allowed.", "code": "method_not_allowed"}), 405


@app.errorhandler(500)
def server_error(_):
    return jsonify({"error": "Server error.", "code": "server_error"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
