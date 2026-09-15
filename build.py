#!/usr/bin/env python3
"""Build Aurelia's X queue dashboard from links.txt.

Reads links.txt, fetches each post from X's syndication endpoint (falling back
to oEmbed), caches the raw JSON and images in cache/, and writes dashboard.html
by filling template.html. No API keys needed.
"""
import base64, html, json, os, re, sys, urllib.parse, urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LINKS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "links.txt")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "dashboard.html")
SITE = os.path.join(HERE, "docs")   # GitHub Pages serves this folder as the site
CACHE = os.path.join(HERE, "cache")
TEMPLATE = os.path.join(HERE, "template.html")
# X marks these "languages" on posts with no real prose (links, emoji, numbers).
NO_TRANSLATE = {"en", "und", "qme", "qst", "zxx", "art", "qht", "qam", "qct", ""}
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def parse_links():
    items = []
    for raw in open(LINKS, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url, note = (line.split(" -- ", 1) + [""])[:2]
        m = re.search(r"(?:x|twitter)\.com/([^/]+)/status/(\d+)", url)
        if not m:
            print(f"skip (not a post link): {line}", file=sys.stderr)
            continue
        items.append({"id": m.group(2), "handle": m.group(1), "note": note.strip(),
                      "url": f"https://x.com/{m.group(1)}/status/{m.group(2)}"})
    return items

def get_tweet(tid):
    path = os.path.join(CACHE, f"{tid}.json")
    if os.path.exists(path):
        return json.load(open(path))
    data = None
    try:
        raw = fetch(f"https://cdn.syndication.twimg.com/tweet-result?id={tid}&token=a")
        if raw.strip():
            data = json.loads(raw)
            if data.get("__typename") == "TweetTombstone":
                data = None
    except Exception as e:
        print(f"  syndication failed for {tid}: {e}", file=sys.stderr)
    if not data:
        fx = get_fx("i", tid)
        if fx:
            data = fx_to_syndication(fx)
    if not data:
        try:
            q = urllib.parse.urlencode({"url": f"https://x.com/i/status/{tid}", "omit_script": "true", "dnt": "true"})
            o = json.loads(fetch(f"https://publish.x.com/oembed?{q}"))
            text = re.sub(r"<[^>]+>", "", re.search(r"<p[^>]*>(.*?)</p>", o["html"], re.S).group(1))
            data = {"id_str": tid, "text": html.unescape(text.replace("<br>", "\n")),
                    "user": {"name": o["author_name"], "screen_name": o["author_url"].rstrip("/").split("/")[-1]},
                    "created_at": None, "entities": {}, "_source": "oembed"}
        except Exception as e:
            print(f"  oembed failed for {tid}: {e}", file=sys.stderr)
            return None
    json.dump(data, open(path, "w"), indent=1)
    return data

def get_fx(handle, tid, lang=None):
    """FxTwitter's public API: full text for long posts and, with a lang
    suffix, a translation. Cached; returns the `tweet` object or None."""
    suffix = f".fx.{lang}" if lang else ".fx"
    path = os.path.join(CACHE, f"{tid}{suffix}.json")
    if os.path.exists(path):
        return json.load(open(path))
    try:
        url = f"https://api.fxtwitter.com/{handle}/status/{tid}" + (f"/{lang}" if lang else "")
        d = json.loads(fetch(url))
        t = d.get("tweet")
        if not t:
            return None
        json.dump(t, open(path, "w"), indent=1)
        return t
    except Exception as e:
        print(f"  fxtwitter failed for {tid}: {e}", file=sys.stderr)
        return None

def fx_to_syndication(fx):
    """Shape an FxTwitter tweet like the syndication endpoint so one renderer serves both."""
    a = fx.get("author") or {}
    media = fx.get("media") or {}
    photos = [{"url": m["url"].split("?")[0]} for m in media.get("photos") or [] if m.get("url")]
    videos = [{"type": "animated_gif" if m.get("type") == "gif" else "video", "id_str": str(m.get("id", "")),
               "media_url_https": m.get("thumbnail_url"),
               "video_info": {"duration_millis": int((m.get("duration") or 0) * 1000), "aspect_ratio": [m.get("width", 16), m.get("height", 9)],
                              "variants": [{"content_type": "video/mp4", "bitrate": f.get("bitrate"), "url": f["url"]}
                                           for f in m.get("formats", []) if f.get("container") == "mp4"]}}
              for m in media.get("videos") or []]
    return {"id_str": str(fx["id"]), "text": fx.get("text", ""), "lang": fx.get("lang"),
            "created_at": datetime.utcfromtimestamp(fx["created_timestamp"]).strftime("%Y-%m-%dT%H:%M:%S") if fx.get("created_timestamp") else None,
            "user": {"name": a.get("name", ""), "screen_name": a.get("screen_name", ""), "profile_image_url_https": (a.get("avatar_url") or "").replace("_200x200", "_normal")},
            "entities": {}, "photos": photos, "mediaDetails": videos, "favorite_count": fx.get("likes"),
            "conversation_count": fx.get("replies"), "note_tweet": None, "_full": True,
            "quoted_tweet": fx_to_syndication(fx["quote"]) if fx.get("quote") else None}

def data_uri(url, small=True):
    """Fetch an image and return it as a data URI (cached). Artifact CSP blocks
    remote images, so they must ship inside the page."""
    if small and "pbs.twimg.com/media/" in url and "?" not in url:
        ext = url.rsplit(".", 1)[-1]
        url = url.rsplit(".", 1)[0] + f"?format={ext}&name=small"
    key = re.sub(r"[^A-Za-z0-9]+", "_", url)[-80:]
    path = os.path.join(CACHE, key + ".b64")
    if os.path.exists(path):
        return open(path).read()
    try:
        raw = fetch(url)
    except Exception as e:
        print(f"  image failed {url}: {e}", file=sys.stderr)
        return ""
    mime = "image/png" if raw[:4] == b"\x89PNG" else "image/webp" if raw[8:12] == b"WEBP" else "image/jpeg"
    uri = f"data:{mime};base64," + base64.b64encode(raw).decode()
    open(path, "w").write(uri)
    return uri

def _link(m):
    url = m.group(1)
    disp = re.sub(r"^https?://(www\.)?", "", url)[:60]
    return f'<a href="{url}" target="_blank" rel="noopener">{disp}</a>'

MAX_VIDEO_BITRATE = 1_300_000   # ~720p; keeps clips a few MB each
MAX_VIDEO_BYTES = 14 * 1024 * 1024
VIDEO_DIR = os.path.join(CACHE, "video")
FILES = {}   # published path -> local file, written to files.json for the Artifact publish

def get_video(m):
    """Download the best mp4 variant under the size cap. Returns a page-ready
    dict or None (the page then says the video is on X)."""
    vi = m.get("video_info") or {}
    variants = sorted([v for v in vi.get("variants", []) if v.get("content_type") == "video/mp4" and v.get("bitrate") is not None],
                      key=lambda v: -v["bitrate"])
    variants = [v for v in variants if v["bitrate"] <= MAX_VIDEO_BITRATE] or variants[-1:]
    # Stable name: the numeric id in the video URL, else the thumbnail slug.
    vid_id = re.search(r"/(\d{8,})/", variants[0]["url"]) if variants else None
    slug = re.search(r"/media/(\w+)", m.get("media_url_https", "") or "")
    mid = str(m.get("id_str") or (vid_id.group(1) if vid_id else "") or (slug.group(1) if slug else "") or "video")
    os.makedirs(VIDEO_DIR, exist_ok=True)
    local = os.path.join(VIDEO_DIR, f"{mid}.mp4")
    if not os.path.exists(local):
        for v in variants:
            try:
                raw = fetch(v["url"], timeout=120)
            except Exception as e:
                print(f"  video failed {v['url'][:60]}: {e}", file=sys.stderr); continue
            if len(raw) <= MAX_VIDEO_BYTES:
                open(local, "wb").write(raw); break
            print(f"  video variant too big ({len(raw)//1024} KB), trying smaller", file=sys.stderr)
    if not os.path.exists(local):
        return None
    pub = f"media/{mid}.mp4"
    FILES[pub] = local
    ar = vi.get("aspect_ratio") or [16, 9]
    return {"src": pub, "poster": data_uri(m["media_url_https"]) if m.get("media_url_https") else "",
            "w": ar[0], "h": ar[1], "gif": m.get("type") == "animated_gif",
            "seconds": round((vi.get("duration_millis") or 0) / 1000)}

def render_text(t):
    """Escape the post text, drop media t.co links, linkify urls, @handles, #tags."""
    text = t.get("text", "")
    ents = t.get("entities") or {}
    for m in (ents.get("media") or []) + (t.get("mediaDetails") or []):
        text = text.replace(m.get("url", "\0"), "")
    urlmap = {u["url"]: u for u in ents.get("urls", [])}
    out = html.escape(text.strip())
    for short, u in urlmap.items():
        disp, exp = html.escape(u.get("display_url", short)), html.escape(u.get("expanded_url", short))
        out = out.replace(html.escape(short), f'<a href="{exp}" target="_blank" rel="noopener">{disp}</a>')
    out = re.sub(r"(?<![\"'>=/])(https?://[^\s<]+[^\s<.,;:!?)\]])", _link, out)
    out = re.sub(r"(^|[^\w/])@(\w{1,15})", r'\1<a href="https://x.com/\2" target="_blank" rel="noopener">@\2</a>', out)
    out = re.sub(r"(^|\s)#(\w+)", r'\1<a href="https://x.com/hashtag/\2" target="_blank" rel="noopener">#\2</a>', out)
    return out.replace("\n", "<br>")

def fmt_date(iso):
    if not iso:
        return ""
    d = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    return d.strftime("%-d %b %Y")

def build_post(t):
    u = t.get("user", {})
    tid, handle, lang = t["id_str"], u.get("screen_name", "i"), t.get("lang") or ""
    truncated = bool(t.get("note_tweet"))
    if truncated:
        fx = get_fx(handle, tid)
        if fx and fx.get("text"):
            t = dict(t, text=fx["text"], entities={}, mediaDetails=[])
            truncated = False
    translation = None
    if lang not in NO_TRANSLATE:
        fx = get_fx(handle, tid, "en")
        tr = (fx or {}).get("translation")
        if tr and tr.get("text") and tr.get("source_lang") != "en":
            translation = {"html": render_text({"text": tr["text"], "entities": {}}),
                           "from": tr.get("source_lang_en") or tr.get("source_lang") or lang}
    photos = [data_uri(p["url"]) for p in (t.get("photos") or [])]
    vids = [m for m in (t.get("mediaDetails") or []) if m.get("type") in ("video", "animated_gif")]
    videos = [get_video(m) for m in vids]
    return {
        "id": t["id_str"], "name": u.get("name", ""), "handle": u.get("screen_name", ""),
        "avatar": data_uri(u["profile_image_url_https"], small=False) if u.get("profile_image_url_https") else "",
        "date": fmt_date(t.get("created_at")), "ts": t.get("created_at") or "", "html": render_text(t),
        "truncated": truncated, "translation": translation, "lang": lang, "photos": [p for p in photos if p],
        "videos": [v for v in videos if v], "video_on_x": any(v is None for v in videos), "likes": t.get("favorite_count"),
        "replies": t.get("conversation_count", t.get("reply_count")),
        "url": f"https://x.com/{u.get('screen_name','i')}/status/{t['id_str']}",
    }

def main():
    items = parse_links()
    posts = []
    for it in items:
        print(f"fetching @{it['handle']} {it['id']} …")
        t = get_tweet(it["id"])
        if not t:
            posts.append({"id": it["id"], "handle": it["handle"], "url": it["url"], "note": it["note"],
                          "missing": True})
            continue
        p = build_post(t)
        p["note"] = it["note"]
        if t.get("quoted_tweet"):
            p["quoted"] = build_post(t["quoted_tweet"])
        posts.append(p)
    posts.sort(key=lambda p: p.get("ts", ""), reverse=True)   # newest first, like a timeline; unfetchable posts last
    built = datetime.now().strftime("%-d %b %Y")
    payload = json.dumps({"built": built, "posts": posts}, ensure_ascii=False).replace("</", "<\\/")
    page = open(TEMPLATE, encoding="utf-8").read().replace("/*__QUEUE_DATA__*/", payload)
    open(OUT, "w", encoding="utf-8").write(page)          # fragment, for the claude.ai Artifact
    # Standalone site for GitHub Pages: full document plus the video files.
    import shutil
    os.makedirs(os.path.join(SITE, "media"), exist_ok=True)
    full = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<meta name=\"robots\" content=\"noindex\">\n"
            + page.split("</style>", 1)[0].replace('<meta charset="utf-8">', "") + "</style>\n</head>\n<body>\n"
            + page.split("</style>", 1)[1] + "\n</body>\n</html>\n")
    open(os.path.join(SITE, "index.html"), "w", encoding="utf-8").write(full)
    for pub, local in FILES.items():
        dest = os.path.join(SITE, pub)
        if not os.path.exists(dest):
            shutil.copyfile(local, dest)
    for f in os.listdir(os.path.join(SITE, "media")):
        if "media/" + f not in FILES:
            os.remove(os.path.join(SITE, "media", f))
    print(f"wrote {SITE}/index.html for GitHub Pages")
    manifest = os.path.join(os.path.dirname(OUT), "files.json")
    json.dump(FILES, open(manifest, "w"), indent=1)
    ok = sum(1 for p in posts if not p.get("missing"))
    if FILES:
        print(f"{len(FILES)} video file(s) to publish alongside the page, listed in {manifest}")
    print(f"wrote {OUT}: {ok}/{len(posts)} posts, {len(page)//1024} KB")

if __name__ == "__main__":
    main()
