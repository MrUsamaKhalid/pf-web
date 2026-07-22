"""Regression tests for the PropertyFinder photo downloader backend.

    python test_app.py          # fast: no network, no PropertyFinder traffic
    python test_app.py --live   # also runs a real listing end to end

The offline half covers the rules that break quietly: filename generation, the
client-supplied name sanitiser, option coercion, and the URL guard. Each of
these has already caused a real bug once.
"""

import io
import json
import re
import sys
import zipfile

import app as backend

LIVE_LISTING = ("https://www.propertyfinder.ae/en/plp/buy/apartment-for-sale-dubai-difc-"
                "park-towers-park-tower-b-101268660.html")
# A listing that has been removed. PropertyFinder answers with its search page,
# which is full of other properties' photos and metadata.
DEAD_LISTING = ("https://www.propertyfinder.ae/en/plp/buy/apartment-for-sale-dubai-"
                "arjan-plazzo-residence-13277893.html")

failures = []


def check(name, cond, detail=""):
    print(("  pass  " if cond else "  FAIL  ") + name + (f"  -- {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def eq(name, got, want):
    check(name, got == want, f"got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- #
def test_url_guard():
    print("\nURL guard")
    for bad in ["https://propertyfinder.ae.attacker.com/x",
                "https://evil.com/propertyfinder.ae",
                "https://www.propertyfinder.ae.co/x",
                "https://notpropertyfinder.ae/x"]:
        check(f"rejects {bad[:44]}", not backend.host_ok(bad))
    for good in ["https://www.propertyfinder.ae/en/plp/buy/x.html",
                 "https://propertyfinder.ae/x",
                 "https://static.shared.propertyfinder.ae/media/x.jpg",
                 "https://www.propertyfinder.qa/x"]:
        check(f"accepts {good[:44]}", backend.host_ok(good))


def test_listing_slug():
    print("\nArchive naming")
    tower = ("https://www.propertyfinder.ae/en/plp/buy/"
             "apartment-for-sale-dubai-arjan-skyz-by-danube-%s.html")
    # The bug this guards: the trailing id is the only difference between units
    # in one building, so stripping it collapsed four downloads into one name.
    ids = ["112758643", "114173737", "101298558"]
    slugs = [backend.slug_from_url(tower % i) for i in ids]
    check("same-tower URLs stay distinct", len(set(slugs)) == 3, slugs)

    eq("title drives the name",
       backend.listing_slug({"title": "BRAND NEW | MIRACLE GARDEN VIEW | LOWER PRICE"}, tower % ids[0]),
       "brand-new-miracle-garden-view-lower-price")
    eq("punctuation and emoji stripped",
       backend.listing_slug({"title": 'Villa <script> |:*?"\\/ 🏠 Emoji'}, tower % ids[0]),
       "villa-script-emoji")
    eq("traversal cannot survive slugify",
       backend.listing_slug({"title": "../../etc/passwd"}, tower % ids[0]), "etc-passwd")
    check("non-Latin title falls back to the URL",
          backend.listing_slug({"title": "شقة للبيع في دبي"}, tower % ids[0]).startswith("apartment-"))
    check("empty title falls back to the URL",
          backend.listing_slug({"title": ""}, tower % ids[0]).startswith("apartment-"))
    for reserved in ["CON", "aux", "LPT1", "nul"]:
        s = backend.listing_slug({"title": reserved}, tower % ids[0])
        check(f"reserved device name {reserved!r} defused", s.lower() not in backend._WIN_RESERVED, s)
    check("length is bounded",
          len(backend.listing_slug({"title": "A" * 500}, tower % ids[0])) <= 70)


def test_safe_zip_name():
    print("\nClient-supplied name sanitiser")
    eq("plain name kept", backend.safe_zip_name("My Listing.zip"), "My Listing.zip")
    eq("adds the extension", backend.safe_zip_name("My Listing"), "My Listing.zip")
    eq("strips directories", backend.safe_zip_name("../../../etc/passwd.zip"), "passwd.zip")
    eq("strips backslash directories", backend.safe_zip_name(r"..\..\windows\evil.zip"), "evil.zip")
    eq("trailing dots removed", backend.safe_zip_name("trailing dots..."), "trailing dots.zip")
    eq("illegal characters removed", backend.safe_zip_name('bad<>:"|?*chars.zip'), "badchars.zip")
    for bad in ["CON.zip", "nul", "", "   ", "...", None, 123]:
        eq(f"rejects {bad!r}", backend.safe_zip_name(bad), None)
    long = backend.safe_zip_name("x" * 300 + ".zip")
    check("length bounded", len(long) <= 125, len(long))
    check("newline cannot reach the header", "\n" not in (backend.safe_zip_name("a\nb.zip") or ""))


def test_options():
    print("\nOption normalisation")
    opts, notices, err = backend.normalize_options(None)
    check("None yields defaults", opts == backend.DEFAULTS and err is None)

    opts, notices, err = backend.normalize_options({"format": "tiff"})
    eq("bad enum falls back", opts["format"], "jpeg")
    check("bad enum is reported", any("format" in n.lower() for n in notices), notices)

    opts, _, _ = backend.normalize_options({"max_images": 99999})
    eq("max_images clamped high", opts["max_images"], backend.OPTION_SPEC["max_images"]["max"])
    opts, _, _ = backend.normalize_options({"max_images": -5})
    eq("max_images clamped low", opts["max_images"], backend.OPTION_SPEC["max_images"]["min"])
    opts, _, _ = backend.normalize_options({"max_images": "12"})
    eq("numeric string accepted", opts["max_images"], 12)

    opts, _, _ = backend.normalize_options({"unknown_key": True})
    check("unknown keys dropped", "unknown_key" not in opts)

    _, _, err = backend.normalize_options({"property": False, "community": False})
    check("no galleries is an error", err is not None, err)

    opts, notices, err = backend.normalize_options("not a dict")
    check("non-dict payload survives", opts == backend.DEFAULTS and err is None)


def test_pattern_parsing():
    print("\nNaming pattern validation")
    ok = lambda p: backend.parse_pattern(p)[0]           # noqa: E731
    notes = lambda p: backend.parse_pattern(p)[1]        # noqa: E731

    eq("default kept", ok("{index}"), "{index}")
    eq("case-insensitive tokens", ok("{Index}"), "{index}")
    eq("empty means default", ok(""), backend.DEFAULT_NAME_PATTERN)
    check("empty is not an error", not notes(""))
    eq("unknown token rejected", ok("{bogus}-{index}"), backend.DEFAULT_NAME_PATTERN)
    eq("unmatched brace rejected", ok("{inde-{index}"), backend.DEFAULT_NAME_PATTERN)
    eq("non-string rejected", ok(None), backend.DEFAULT_NAME_PATTERN)
    eq("over-long rejected", ok("x" * 200), backend.DEFAULT_NAME_PATTERN)
    check("missing {index} warns", any("index" in n for n in notes("{listing}")))
    check("slashes warn", any("folder" in n.lower() for n in notes("a/b{index}")))
    check("valid multi-token accepted", ok("{ref}-{listing}-{index}") == "{ref}-{listing}-{index}")


def test_render_names():
    print("\nName rendering")
    meta = {"title": "Spacious 2BR | Full Marina View — Vacant Now",
            "reference": "AP8297-3", "agent": "Sara Ahmed"}

    def render(pattern, index=1, pad=2, m=None):
        plan = backend.build_name_plan({"naming": pattern}, m if m is not None else meta, pad)
        return backend.render_name(plan, index)

    eq("default is the padded index", render("{index}"), "01")
    eq("ref keeps its case", render("{ref}-{index}"), "AP8297-3-01")
    check("title folded", render("{listing}-{index}").startswith("Spacious-2BR-Full-Marina-View"),
          render("{listing}-{index}"))
    check("date token expands",
          re.fullmatch(r"\d{4}-\d{2}-\d{2}-01", render("{date}-{index}")) is not None,
          render("{date}-{index}"))

    # missing sources must not leave debris behind
    bare = {"title": "", "reference": "", "agent": ""}
    eq("missing tokens collapse cleanly", render("{listing}-{ref}-{index}", m=bare), "01")
    eq("leading separator stripped", render("{ref}-{index}", m=bare), "01")
    eq("all tokens empty falls back to index", render("{listing}{ref}{agent}", m=bare), "01")

    arabic = {"title": "شقة فاخرة في دبي مارينا", "reference": "", "agent": ""}
    eq("non-Latin title yields the index", render("{listing}-{index}", m=arabic), "01")

    eq("traversal in a pattern is neutralised", render("../../{index}"), "01")
    eq("drive letter neutralised", render(r"C:\Windows\{index}"), "C-Windows-01")
    check("emoji stripped", render("{listing}-{index}", m={"title": "🔥 HOT ✅ DEAL"}) == "HOT-DEAL-01",
          render("{listing}-{index}", m={"title": "🔥 HOT ✅ DEAL"}))
    eq("NFKD exposes a device name, which is then dodged",
       render("{listing}", m={"title": "COM²"}), "COM2_")
    eq("sharp s survives", render("{listing}", m={"title": "Straße"}), "Strasse")


def test_unique_arc():
    print("\nCollision resolution")
    used = {"_info.txt", "_info.json"}
    a = backend.unique_arc("property", "villa", "jpg", "s" * 64, used)
    b = backend.unique_arc("property", "villa", "jpg", "t" * 64, used)
    c = backend.unique_arc("property", "VILLA", "jpg", "u" * 64, used)
    eq("first is clean", a, "property/villa.jpg")
    eq("second disambiguated", b, "property/villa-2.jpg")
    eq("case-insensitive collision caught", c, "property/VILLA-3.jpg")

    used2 = {"_info.txt", "_info.json"}
    hit = backend.unique_arc("", "_info", "txt", "s" * 64, used2)
    check("cannot land on the manifest", hit.lower() != "_info.txt", hit)

    used3 = set()
    long_arc = backend.unique_arc("property", "x" * 300, "jpg", "s" * 64, used3)
    check("path budget respected", len(long_arc) <= backend.MAX_ARC_PATH, len(long_arc))
    long_arc2 = backend.unique_arc("property", "x" * 300, "jpg", "t" * 64, used3)
    check("disambiguator survives truncation", long_arc2.endswith("-2.jpg"), long_arc2)
    check("truncated names still differ", long_arc != long_arc2)

    used4 = set()
    res = backend.unique_arc("", "console"[:3], "jpg", "s" * 64, used4)
    check("reserved name dodged after clipping", res.lower() != "con.jpg", res)


def test_http_contract():
    print("\nHTTP contract")
    c = backend.app.test_client()
    r = c.get("/")
    check("health ok", r.status_code == 200 and r.get_json()["status"] == "ok")
    caps = c.get("/capabilities").get_json()
    check("capabilities lists every option", set(caps["options"]) == set(backend.OPTION_SPEC))
    check("capabilities defaults match spec", caps["defaults"] == backend.DEFAULTS)

    for payload, code in [({}, "bad_request"),
                          ({"url": "https://evil.com/x"}, "bad_url"),
                          ({"url": "https://x.propertyfinder.ae/a.html",
                            "options": {"property": False, "community": False}}, "bad_option")]:
        r = c.post("/scrape", json=payload)
        eq(f"{code} -> 400", (r.status_code, r.get_json().get("code")), (400, code))

    r = c.get("/zip/deadbeef")
    eq("unknown token -> 404 expired", (r.status_code, r.get_json().get("code")), (404, "expired"))


def _run(client, url, options=None):
    r = client.post("/scrape", json={"url": url, "options": options or {}})
    frames = [json.loads(l) for l in r.get_data(as_text=True).splitlines() if l.strip()]
    return frames, next((f for f in frames if f["type"] == "done"), None)


def test_live():
    print("\nLive listing (network)")
    c = backend.app.test_client()

    frames, done = _run(c, DEAD_LISTING)
    err = next((f for f in frames if f["type"] == "error"), None)
    check("removed listing errors instead of scraping search results",
          done is None and err and err["code"] == "not_a_listing", err)

    frames, done = _run(c, LIVE_LISTING, {"max_images": 8})
    check("live listing succeeds", done is not None)
    if not done:
        return
    check("both galleries found", done["property"] > 0 and done["community"] > 0, done)
    eq("count is the sum", done["count"], done["property"] + done["community"])
    check("archive named from the title", done["filename"].endswith(".zip"), done["filename"])
    check("token handoff", "download" in done and "zip" not in done)

    r = c.get(done["download"])
    check("zip downloads", r.status_code == 200)
    z = zipfile.ZipFile(io.BytesIO(r.get_data()))
    names = z.namelist()
    check("zip is valid", z.testzip() is None)
    check("grouped into folders",
          all(n.startswith(("property/", "community/", "_info")) for n in names), names[:4])
    check("no traversal in entry names", not any(n.startswith(("/", "\\")) or ".." in n for n in names))
    check("default format is jpeg", all(n.endswith(".jpg") for n in names if not n.startswith("_")))
    biggest = max(z.getinfo(n).file_size for n in names if not n.startswith("_"))
    check("full resolution, not thumbnails", biggest > 50_000, f"{biggest} bytes")

    r = c.get(done["download"] + "?name=" + "custom%20name.zip")
    check("client can name the archive",
          "filename*=UTF-8''custom%20name.zip" in r.headers["Content-Disposition"],
          r.headers.get("Content-Disposition"))
    r = c.get(done["download"] + "?name=../../evil.zip")
    check("client name cannot traverse",
          ".." not in r.headers["Content-Disposition"], r.headers.get("Content-Disposition"))

    _, done2 = _run(c, LIVE_LISTING, {"community": False, "structure": "flat", "max_images": 4})
    if done2:
        eq("community excluded", done2["community"], 0)
        n2 = zipfile.ZipFile(io.BytesIO(c.get(done2["download"]).get_data())).namelist()
        check("flat has no folders", not any("/" in n for n in n2), n2)


if __name__ == "__main__":
    test_url_guard()
    test_listing_slug()
    test_safe_zip_name()
    test_options()
    test_pattern_parsing()
    test_render_names()
    test_unique_arc()
    test_http_contract()
    if "--live" in sys.argv:
        test_live()
    else:
        print("\n(skipping live listing tests; pass --live to include them)")

    print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED: " + ", ".join(failures)))
    sys.exit(1 if failures else 0)
