"""
PropertyFinder Photo ZIP Downloader — backend v3

Reads a PropertyFinder listing's __NEXT_DATA__ (server-rendered JSON) and pulls
the full-resolution gallery photos, split into the same tabs the site shows:

    images.property[] (+ images.tower[])  ->  property/   in the ZIP
    images.community[]                    ->  community/  in the ZIP

PropertyFinder server-renders __NEXT_DATA__, so a plain HTTP GET is enough and
there is no browser anywhere on this path.

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
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, quote

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import requests

APP_VERSION = "3.1.0"

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
MAX_PER_GROUP = 150
MAX_TOTAL_BYTES = 100 * 1024 * 1024   # archives stream to disk, so this is a
                                      # bandwidth/disk guard, not a memory one
JOB_DEADLINE = 140                    # self-abort before gunicorn's timeout
ZIP_TTL = 600                         # download token lifetime (seconds)

MAX_CONCURRENT_JOBS = 2
# Per IP, and a brokerage office is ONE IP behind NAT — ten agents at 40/hour
# would have been four each. Sized instead for a whole office's real day
# (~50/hour across everyone, with headroom) while still bounding a runaway
# script. Bandwidth is not the binding constraint here: ~10 MB a listing against
# Render's allowance leaves far more room than this cap allows through.
RATE_CAPACITY = 150                   # jobs per IP...
RATE_REFILL_PER_SEC = 150 / 3600.0    # ...refilling over an hour

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
# File naming
# --------------------------------------------------------------------------- #
DEFAULT_NAME_PATTERN = "{index}"
# {agent} is the PropertyFinder listing agent — often a rival brokerage's staff.
# {you} is whoever here ran the download, which is the name that usually belongs
# on our own files.
NAME_TOKENS = ("you", "listing", "ref", "agent", "index", "date")
MAX_PATTERN_LEN = 120
MAX_AGENT_LEN = 40

# Windows Explorer still unpacks ZIPs through the legacy 260-char MAX_PATH, and
# "Extract All" creates a folder named after the archive before writing a single
# file. Capping the path *inside* the archive at 120 leaves ~140 for a
# destination like "C:\Users\usama\OneDrive - Sykon Properties\Downloads\<zip>\".
MAX_ARC_PATH = 120
MAX_SEGMENT = 80        # NTFS/APFS/ext4 all stop at 255; 80 keeps names legible
SLUG_MAX = 48           # the by_listing folder AND the .zip file name
TOKEN_MAX = {"listing": 40, "ref": 24, "agent": 24, "you": 24, "date": 10, "index": 8}

# The UAE has never observed DST and has been UTC+4 since 1972, so a fixed
# offset is exactly as correct as zoneinfo here — and it does not depend on the
# container image shipping tzdata, which the slim Python images do not. Render
# runs UTC, so without this a download at 02:00 Dubai time is stamped yesterday.
DUBAI_TZ = timezone(timedelta(hours=4))


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
    # Published verbatim by /capabilities, so the tokens and the length cap
    # reach the UI without a second endpoint to keep in sync.
    "naming":      {"type": "pattern", "default": DEFAULT_NAME_PATTERN,
                    "label": "Photo file names",
                    "tokens": list(NAME_TOKENS), "max_len": MAX_PATTERN_LEN,
                    "note": "The extension is added from the image itself."},
    # Free text rather than an account: this app has no sign-in, and one
    # optional name is not worth reintroducing one.
    "agent_name":  {"type": "text", "default": "", "label": "Your name",
                    "max_len": MAX_AGENT_LEN,
                    "note": "Fills the {you} token and is recorded in the info file."},
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
        elif spec["type"] == "text":
            if isinstance(value, str):
                # Kept as typed — the sanitiser runs at naming time, so the
                # info file can still show "Usama Khalid" rather than a slug.
                opts[key] = value.strip()[:spec["max_len"]]
            elif value is None:
                opts[key] = ""
            else:
                notices.append(f"{spec['label']}: expected text, leaving it blank.")
        elif spec["type"] == "pattern":
            # A naming pattern is never unrunnable — anything we cannot honour
            # falls back to the default and says so.
            opts[key], notes = parse_pattern(value)
            notices.extend(notes)

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


_TOKEN_RE = re.compile(r"\{([A-Za-z_]{0,16})\}")
# Illegal on Win32, plus ':' (the drive marker in "C:" and the classic Finder
# separator), both path separators, and all whitespace — this app speaks
# hyphen-case, and spaces round-trip badly through URLs and shells.
_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*\s]+')
_KEEP_RE = re.compile(r"[^A-Za-z0-9._-]+")

# NFKD's blind spots: these have no decomposition, so without the table they
# vanish silently and "Straße" becomes "Strae".
_ASCII_FALLBACKS = {"ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
                    "đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "þ": "th",
                    "Þ": "Th", "ð": "d", "Ð": "D", "œ": "oe", "Œ": "OE"}

# Reserved by Win32 with *any* extension — CON.jpg is still the console. The
# '$' names can never match, because '$' is stripped by the allowlist before
# _dodge_reserved runs; they are listed for completeness, not defence.
_WIN_RESERVED = frozenset(
    ["con", "prn", "aux", "nul", "clock$", "conin$", "conout$"]
    + [f"com{i}" for i in range(10)] + [f"lpt{i}" for i in range(10)]
)


def fold_ascii(value):
    """Latin accents to ASCII; everything else non-ASCII dropped.

    Chosen over preserving UTF-8: a ZIP only carries non-ASCII member names if
    the reader honours general-purpose bit 11, and the tooling a brokerage
    actually uses (older Explorer, portal bulk-uploaders, FTP clients) often
    does not — the failure mode is silent mojibake across forty photos.
    Transliterating Arabic would need a table nobody agrees on and produces
    "shq fkhr fy dby", unreadable to Arabic and English speakers alike. Nothing
    is lost: the untouched title stays in _info.txt and the meta frame.
    """
    s = "".join(_ASCII_FALLBACKS.get(c, c) for c in str(value))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.encode("ascii", "ignore").decode("ascii")


def _dodge_reserved(stem):
    """CON -> CON_. Suffixed rather than prefixed so sort order survives.

    Runs after the fold on purpose: NFKD turns COM² into COM2, a device name the
    raw string was hiding. It runs again after truncation, because clipping
    "console" to fit a path budget can re-expose "con".
    """
    if stem.split(".", 1)[0].lower() in _WIN_RESERVED:
        return stem + "_"
    return stem


def safe_segment(value, max_len=MAX_SEGMENT):
    """One path segment safe on NTFS, APFS, ext4 and inside a ZIP.

    Every rule is load-bearing: control characters break archive tooling,
    <>:"/\\|?* are rejected by Win32, ".." is the parent directory everywhere, a
    leading "." hides the file on macOS/Linux, a leading "-" reads as a flag to
    CLI tools, and a trailing "." or " " is silently eaten by Win32 — which
    would let two names converge into one file *after* extraction, behind the
    back of the collision check.
    """
    s = fold_ascii(value)
    s = _UNSAFE_RE.sub("-", s)
    s = _KEEP_RE.sub("-", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"-{2,}", "-", s)
    s = s.strip(" ._-")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" ._-")
    return _dodge_reserved(s)


def slugify(text, max_len=SLUG_MAX):
    """Lower-case hyphen slug for the archive name and by_listing folder.

    Deliberately lower-cased where safe_segment preserves case: this is the name
    that lands in someone's Downloads folder, while photo names keep their case
    because {ref} is the string agents search their CRM for ("AP8297-3").
    """
    # Dodge last: stripping "._-" would otherwise undo safe_segment's guard and
    # turn "CON_" straight back into the reserved "con".
    return _dodge_reserved(safe_segment(text, max_len).lower().strip("._-"))


def parse_pattern(raw):
    """Validate a naming pattern. Returns (pattern, notices).

    Anything we cannot honour falls back to the default instead of guessing: a
    half-understood pattern names forty files wrong, and the user does not find
    out until the photos are already on a portal.
    """
    label = OPTION_SPEC["naming"]["label"]
    fallback = f"using '{DEFAULT_NAME_PATTERN}'."
    if not isinstance(raw, str):
        return DEFAULT_NAME_PATTERN, [f"{label}: expected text, {fallback}"]

    # {Index} is a typo, not an error — normalise case before validating.
    pattern = _TOKEN_RE.sub(lambda m: "{" + m.group(1).lower() + "}", raw.strip())
    if not pattern:
        # an empty box means "I cleared it", not "I made a mistake"
        return DEFAULT_NAME_PATTERN, []
    if len(pattern) > MAX_PATTERN_LEN:
        return DEFAULT_NAME_PATTERN, [
            f"{label}: longer than {MAX_PATTERN_LEN} characters, {fallback}"]

    unknown = sorted({m.group(1) for m in _TOKEN_RE.finditer(pattern)} - set(NAME_TOKENS))
    if unknown:
        listed = ", ".join("{" + u + "}" for u in unknown)
        return DEFAULT_NAME_PATTERN, [f"{label}: {listed} isn't a token, {fallback}"]

    literal = _TOKEN_RE.sub("", pattern)
    # a stray brace means a mistyped token; expanding the rest would bake
    # "{inde" into every file name
    if "{" in literal or "}" in literal:
        return DEFAULT_NAME_PATTERN, [f"{label}: unmatched {{ or }}, {fallback}"]

    notices = []
    if "/" in literal or "\\" in literal:
        notices.append(f"{label}: folders come from the Folder structure option — "
                       "the slashes were removed.")
    if fold_ascii(literal) != literal:
        # Without this the user types an Arabic or Cyrillic prefix, hears
        # nothing, and every photo comes out as a bare "01.jpg" because the
        # literal folded away to nothing.
        notices.append(f"{label}: file names can only use Latin characters, so "
                       "some of what you typed was removed.")
    if "{index}" not in pattern:
        notices.append(f"{label}: without {{index}} every photo asks for the same "
                       "name, so repeats get -2, -3 and so on.")
    return pattern, notices


def naming_notes(pattern, meta, opts=None):
    """One notice per job for tokens that cannot be filled — said once."""
    notes = []
    # {you} comes from settings, so an empty one is the operator's own doing and
    # deserves a different sentence from "this listing has no reference".
    if "{you}" in pattern and not safe_segment((opts or {}).get("agent_name") or "",
                                               TOKEN_MAX["you"]):
        notes.append("Your name is blank in options, so {you} was left out of "
                     "the file names.")
    missing = []
    for token, key, human in (("{listing}", "title", "a usable title"),
                              ("{ref}", "reference", "an agency reference"),
                              ("{agent}", "agent", "an agent name")):
        if token in pattern and not safe_segment(meta.get(key) or "",
                                                 TOKEN_MAX[token[1:-1]]):
            missing.append((token, human))
    if missing:
        notes.append(f"This listing has no {' or '.join(h for _, h in missing)} — "
                     f"{', '.join(t for t, _ in missing)} was left out of the file names.")
    return notes


def build_name_plan(opts, meta, pad):
    """Freeze everything that does not vary per photo.

    {date} is stamped once for the whole archive: a job starting at 23:59:50 and
    running its full deadline must not put two dates in one folder.
    """
    return {
        "pattern": opts.get("naming") or DEFAULT_NAME_PATTERN,
        "pad": pad,
        "values": {
            "listing": safe_segment(meta.get("title") or "", TOKEN_MAX["listing"]),
            "ref": safe_segment(meta.get("reference") or "", TOKEN_MAX["ref"]),
            "agent": safe_segment(meta.get("agent") or "", TOKEN_MAX["agent"]),
            # comes from the operator's own settings, not from the listing
            "you": safe_segment(opts.get("agent_name") or "", TOKEN_MAX["you"]),
            "date": datetime.now(DUBAI_TZ).strftime("%Y-%m-%d"),
        },
    }


def render_name(plan, index):
    """Expand the pattern for one photo. Returns a stem, never a path."""
    def expand(m):
        key = m.group(1)
        if key == "index":
            return f"{index:0{plan['pad']}d}"
        return plan["values"].get(key, "")

    # Sanitise the assembled stem rather than the pieces: that is what collapses
    # the "--" a missing {ref} leaves in "{listing}-{ref}-{index}", and strips
    # the orphan leading "-" out of "{ref}-{index}".
    stem = safe_segment(_TOKEN_RE.sub(expand, plan["pattern"]))
    # Every token this listing could fill came back empty. {index} always
    # resolves, so it is the honest floor.
    return stem or f"{index:0{plan['pad']}d}"


def unique_arc(folder, stem, ext, sha, used):
    """A ZIP member path that cannot collide with one already in `used`.

    Dedupe-by-hash upstream removes duplicate *photos*; this removes duplicate
    *names*, a different failure: "{listing}" with no {index} asks for the same
    name forty times. Compared case-insensitively because NTFS and a default
    APFS volume are — two members differing only in case are two ZIP entries but
    one file after extraction, and the second silently wins.
    """
    prefix = f"{folder}/" if folder else ""
    # -1 for the dot, -1 so _dodge_reserved's underscore can never push the
    # finished path past MAX_ARC_PATH.
    budget = max(8, MAX_ARC_PATH - len(prefix) - len(ext) - 2)
    for n in range(1, 1000):
        suffix = "" if n == 1 else f"-{n}"
        # the disambiguator is reserved out of the budget, so truncation can
        # never be the thing that eats it
        head = stem[:budget - len(suffix)].rstrip(" ._-") or "photo"
        arc = f"{prefix}{_dodge_reserved(head + suffix)}.{ext}"
        if arc.lower() not in used:
            # The budget floor above is a guard, not a guarantee. Assert the
            # invariant the cap exists to hold, so a future change to SLUG_MAX
            # or the group names cannot quietly produce a path Explorer refuses
            # halfway through an extraction.
            if len(arc) > MAX_ARC_PATH:
                app.logger.warning("arc path over budget (%d): %s", len(arc), arc)
                over = len(arc) - MAX_ARC_PATH
                head = head[:max(1, len(head) - over)].rstrip(" ._-") or "photo"
                arc = f"{prefix}{_dodge_reserved(head + suffix)}.{ext}"
                if arc.lower() in used:
                    continue
            used.add(arc.lower())
            return arc
    # Unreachable — an archive holds at most MAX_PER_GROUP * 2 photos. Kept as
    # the proof this terminates: the sha is unique per entry precisely because
    # the hash dedupe ran before naming.
    head = stem[:budget - 11].rstrip(" ._-") or "photo"
    arc = f"{prefix}{_dodge_reserved(head + '-' + sha[:10])}.{ext}"
    used.add(arc.lower())
    return arc


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
    # The trailing digits are the listing id. Stripping them made every unit in
    # the same building collapse to one identical name, so four downloads from
    # one tower arrived as "name.zip", "name (1).zip", "name (2).zip".
    last = re.sub(r"[^a-z0-9\-]", "", last)
    last = re.sub(r"-{2,}", "-", last).strip("-")
    # The slug is both the by_listing folder and the .zip name, so an unbounded
    # one silently eats the archive's path budget. Clip the description, never
    # the trailing listing id — that id is the only thing telling two units in
    # the same building apart, and clipping from the right ate it.
    if len(last) > SLUG_MAX:
        m = re.search(r"-\d{4,}$", last)
        if m:
            head = last[:m.start()][:max(1, SLUG_MAX - len(m.group(0)))].strip("-")
            last = head + m.group(0)
        else:
            last = last[:SLUG_MAX].strip("-")
    return last or "propertyfinder-photos"


def listing_slug(meta, url):
    """The listing's identity for filenames: its title, else the URL."""
    # slugify already defuses reserved device names, which matters here because
    # this string is a directory under by_listing as well as a file name.
    return slugify(meta.get("title") or "") or slug_from_url(url)


def safe_zip_name(requested):
    """Sanitise a client-supplied archive name.

    The client picks the name so a batch can guarantee unique ones, but it is
    still untrusted input that ends up in a Content-Disposition header and then
    on someone's filesystem.
    """
    if not isinstance(requested, str):
        return None
    name = requested.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch >= " " and ch not in '<>:"|?*\x7f')
    name = re.sub(r"\.zip$", "", name, flags=re.I)
    name = name.strip().strip(".").strip()
    if not name or name.lower() in _WIN_RESERVED:
        return None
    return name[:120].strip() + ".zip"


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
    """Returns (data, last_status). The status separates "this listing is gone"
    from "we were blocked", which are the same failure to the code and very
    different advice to the person holding the link.
    """
    status = None
    for _ in range(PAGE_ATTEMPTS):
        try:
            r = requests.get(url, headers=PAGE_HEADERS, timeout=PAGE_TIMEOUT)
        except requests.RequestException:
            time.sleep(RETRY_BACKOFF)
            continue
        status = r.status_code
        if r.status_code == 200:
            data = _extract_next_data_json(r.text)
            if data is not None:
                return data, status
        if r.status_code in (404, 410):
            return None, status
        time.sleep(RETRY_BACKOFF)
    return None, status


# A headless-Chromium fallback used to live here. It was removed once measured:
# it never fired across 24 live listings spanning buy/rent and three emirates
# (all read in about a second), and PropertyFinder server-renders __NEXT_DATA__
# so there is nothing for a browser to wait for. Worse, it was a liability on
# this instance — Chromium needs roughly 300 MB on top of gunicorn against
# Render Starter's 512 MB, so the one time it did fire it would likely OOM and
# take the whole service down rather than fail a single listing.
#
# _STATS makes its absence observable: if PropertyFinder ever starts blocking
# this IP, direct_fail climbs on /health and the fallback can be restored from
# git history rather than guessed at.
_STATS = {"scrapes": 0, "direct_ok": 0, "direct_fail": 0}
_STATS_LOCK = threading.Lock()


def _stat(key, n=1):
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + n


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
    # who here pulled it — distinct from the listing agent above
    add("Saved by:", opts.get("agent_name") or None)
    add("Format:", "JPEG" if opts["format"] == "jpeg" else "original (WebP where served)")
    add("Names:", opts.get("naming", DEFAULT_NAME_PATTERN))
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
    """Folders stay the app's own vocabulary — the naming pattern only ever
    names the file — so no user input can reach a directory component and a
    "../" has nothing to escape. The slug is re-sanitised anyway: it comes from
    a URL, and defence in depth is free here.
    """
    if opts["structure"] == "flat":
        return ""
    if opts["structure"] == "by_listing":
        listing = slugify(slug, SLUG_MAX) or "listing"
        return f"{listing}/{group}" if group else listing
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
    with _STATS_LOCK:
        stats = dict(_STATS)
    # Counters are per-process and reset on deploy. They exist to answer one
    # question without trawling logs: is the direct read still working?
    return {"status": "ok", "service": "propertyfinder-photo-downloader",
            "version": APP_VERSION, "reads": stats}


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
    # A batch knows all of its names at once, so it - not the server - is the
    # only party that can guarantee they do not collide.
    filename = safe_zip_name(request.args.get("name")) or filename
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
            _stat("scrapes")
            data, page_status = next_data_via_requests(url)
            _stat("direct_ok" if data is not None else "direct_fail")
            if data is None:
                app.logger.warning("direct read failed for %s (status %s)", url, page_status)
                if page_status in (404, 410):
                    yield nd({"type": "error", "code": "listing_gone",
                              "message": "That listing no longer exists on PropertyFinder."})
                else:
                    yield nd({"type": "error", "code": "listing_unreadable",
                              "message": "Could not read this listing right now — "
                                         "try again in a moment."})
                return

            if not looks_like_listing(data):
                yield nd({"type": "error", "code": "not_a_listing",
                          "message": "That link doesn't open a listing — it was probably "
                                     "removed or renamed. Open it in a browser and copy "
                                     "the URL again."})
                return

            meta = extract_meta(data)
            slug = listing_slug(meta, url)
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
            for note in naming_notes(opts["naming"], meta, opts):
                yield nd({"type": "notice", "message": note})

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
            plan = build_name_plan(opts, meta, pad)
            # reserving both manifest names means a photo can never be handed a
            # path that lands on top of the info file
            used = {"_info.txt", "_info.json"}

            fd, zip_path = tempfile.mkstemp(prefix="pfzip-", suffix=".zip")
            os.close(fd)
            manifest_entries = []
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for e in entries:
                    group = e["group"]
                    folder = folder_for(group, opts, slug)
                    stem = render_name(plan, e["index"])
                    if opts["structure"] == "flat" and group:
                        # flat drops both galleries into one namespace, so the
                        # gallery has to survive in the name — this is also what
                        # keeps the default pattern byte-identical to v3.0.0
                        # instead of emitting "01.jpg" and "01-2.jpg".
                        stem = safe_segment(f"{group}-{stem}")
                    arc = unique_arc(folder, stem, e["ext"], e["sha256"], used)
                    zf.write(e["src"], arc)
                    manifest_entries.append({"file": arc, "group": group or "photos",
                                             "bytes": e["bytes"], "sha256": e["sha256"],
                                             "source": e["url"]})
                if opts["info"]:
                    if opts["info_format"] == "json":
                        zf.writestr("_info.json",
                                    build_manifest_json(url, meta, manifest_entries, opts))
                    else:
                        # BOM so a non-ASCII title survives legacy Notepad and
                        # Excel's CSV import. Deliberately not on _info.json —
                        # a BOM breaks strict JSON parsers.
                        zf.writestr("_info.txt", "﻿" + build_manifest_txt(
                            url, meta, n_prop, n_comm, n_other, opts))

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
                # Echoed so the UI can tell "honoured" from "silently dropped by
                # an older backend" — Pages and Render deploy independently.
                "naming": opts["naming"],
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
