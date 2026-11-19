#!/usr/bin/env python3
"""TATHYA: collect news, rewrite it in Hindi with Gemini, build a static site.

Standard library only. Run by GitHub Actions every hour.
  python scripts/update.py          normal run (needs GEMINI_API_KEY)
  python scripts/update.py --demo   build a sample site into ./preview (no network)
"""
import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))

SITE_URL = os.environ.get("SITE_URL", "https://kirtyyyy0503-design.github.io/tathya").rstrip("/")
SITE_NAME = "तथ्य"
TAGLINE = "देश और दुनिया की सटीक ख़बरें, बिना शोर के"
UA = "Mozilla/5.0 (compatible; TathyaBot/1.0)"

MAX_STORIES = 300        # stories kept on the site
MAX_NEW_PER_RUN = 6      # Gemini calls per run (free tier friendly)
PER_SOURCE_LIMIT = 3     # newest items taken from each source per run
MAX_AGE_HOURS = 24       # ignore items older than this
GEMINI_PAUSE = 7         # seconds between Gemini calls
DEDUP_HOURS = 72
MAX_IMG_BYTES = 1_500_000  # skip photos larger than this

CATEGORIES = {
    "राष्ट्रीय": "rashtriya",
    "अंतरराष्ट्रीय": "antarrashtriya",
    "व्यापार": "vyapar",
    "टेक": "tech",
    "खेल": "khel",
}
MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त",
          "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]


def log(*a):
    print(*a, flush=True)


# ----------------------------------------------------------------- fetching

def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "hi,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s):
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>", "\n\n", s)
    s = TAG_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def parse_dt(s):
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_telegram(ch):
    page = http_get(f"https://t.me/s/{ch['channel']}").decode("utf-8", "replace")
    items = []
    for block in page.split("tgme_widget_message_wrap")[1:]:
        m = re.search(r'data-post="([^"]+)"', block)
        t = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        d = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        ph = re.search(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", block)
        if not (m and t):
            continue
        text = strip_html(t.group(1))
        items.append({
            "id": f"tg:{m.group(1)}",
            "source": ch["name"],
            "url": f"https://t.me/{m.group(1)}",
            "text": text,
            "published": parse_dt(d.group(1)) if d else None,
            "kind": "telegram",
            "image": ph.group(1) if ph else None,
            "show_images": ch.get("show_images", False),
        })
    return items


def local(tag):
    return tag.rsplit("}", 1)[-1]


def fetch_rss(src):
    root = ET.fromstring(http_get(src["url"]))
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for ch in el:
            n = local(ch.tag)
            if n == "link":
                if "link" not in f:
                    f["link"] = (ch.get("href") or (ch.text or "")).strip()
            elif n in ("content", "thumbnail") and ch.get("url") and (ch.get("type") or "image").startswith("image"):
                f.setdefault("image", ch.get("url"))
            elif n == "enclosure" and (ch.get("type") or "").startswith("image") and ch.get("url"):
                f.setdefault("image", ch.get("url"))
            elif n in ("title", "description", "summary", "pubDate", "published", "updated", "date"):
                f.setdefault(n, (ch.text or "").strip())
        link = f.get("link")
        title = strip_html(f.get("title", ""))
        if not link or not title:
            continue
        desc = strip_html(f.get("description") or f.get("summary") or "")
        pub = f.get("pubDate") or f.get("published") or f.get("updated") or f.get("date")
        if not f.get("image"):
            im = re.search(r'<img[^>]+src=["\']([^"\']+)', f.get("description") or "")
            if im:
                f["image"] = html.unescape(im.group(1))
        items.append({
            "id": f"rss:{link}",
            "source": src["name"],
            "url": link,
            "title": title,
            "desc": desc,
            "published": parse_dt(pub),
            "kind": "rss",
            "image": f.get("image"),
            "show_images": src.get("show_images", False),
        })
    return items


def fetch_article_text(url):
    page = http_get(url).decode("utf-8", "replace")
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", "", page)
    paras = [strip_html(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page)]
    paras = [p for p in paras if len(p) > 50]
    return "\n".join(paras)[:5000]


def fetch_og_image(url):
    page = http_get(url).decode("utf-8", "replace")[:200000]
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', page))
    return html.unescape(m.group(1)) if m else None


def download_image(url, key):
    """Save the photo inside the repo (stable, fast). Returns 'images/xxx.jpg' or None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = r.read(MAX_IMG_BYTES + 1)
    except Exception as e:
        log("   (photo not downloaded:", e, ")")
        return None
    ext = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(ctype)
    if not ext or len(data) > MAX_IMG_BYTES or len(data) < 2000:
        return None
    name = hashlib.md5(key.encode()).hexdigest()[:12] + ext
    (ROOT / "images").mkdir(exist_ok=True)
    (ROOT / "images" / name).write_bytes(data)
    return f"images/{name}"


def build_source_text(item):
    when = item["published"].astimezone(IST).strftime("%d-%m-%Y %H:%M IST") if item["published"] else "अज्ञात"
    if item["kind"] == "telegram":
        body = item["text"]
    else:
        body = item["title"] + "\n" + item["desc"]
        if len(item["desc"]) < 400:
            try:
                extra = fetch_article_text(item["url"])
                if extra:
                    body += "\n" + extra
            except Exception as e:
                log("  (article text not fetched:", e, ")")
    return f"स्रोत: {item['source']}\nप्रकाशित: {when}\n\n{body}"[:7000]


def collect(cfg, seen):
    per_source = []
    now = datetime.now(timezone.utc)
    jobs = [("telegram", c) for c in cfg.get("telegram", [])] + [("rss", r) for r in cfg.get("rss", [])]
    for kind, src in jobs:
        label = src.get("channel") or src.get("url")
        try:
            items = fetch_telegram(src) if kind == "telegram" else fetch_rss(src)
        except Exception as e:
            log(f"[skip source] {label}: {e}")
            continue
        fresh = []
        for it in items:
            if it["id"] in seen:
                continue
            if it["published"] and now - it["published"] > timedelta(hours=MAX_AGE_HOURS):
                continue
            if kind == "telegram" and len(it["text"]) < 80:
                continue
            fresh.append(it)
        fresh.sort(key=lambda x: x["published"] or now, reverse=True)
        log(f"[ok] {label}: {len(items)} items, {len(fresh)} new")
        per_source.append(fresh[:PER_SOURCE_LIMIT])
    # round-robin so every source gets a turn
    out, i = [], 0
    while any(per_source):
        for lst in per_source:
            if i < len(lst):
                out.append(lst[i])
        i += 1
        if i > PER_SOURCE_LIMIT:
            break
    return out


# ------------------------------------------------------------------- Gemini

class QuotaError(Exception):
    pass


def gemini_json(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set")
    model = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }).encode()
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(25 * (attempt + 1))
                continue
            if e.code == 429:
                raise QuotaError("Gemini quota reached")
            raise RuntimeError(f"Gemini HTTP {e.code}: {e.read()[:200]!r}")
    else:
        raise RuntimeError(f"Gemini failed: {last}")
    try:
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise ValueError("Gemini returned no text (blocked or empty)")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("No JSON in Gemini output")
        return json.loads(m.group(0))


def make_story(data, item):
    if not isinstance(data, dict):
        raise ValueError("not an object")
    headline = str(data.get("headline", "")).strip()
    summary = str(data.get("summary", "")).strip()
    body = str(data.get("body", "")).strip()
    if not (headline and summary and body):
        raise ValueError("missing fields")
    cat = data.get("category")
    if cat not in CATEGORIES:
        cat = "राष्ट्रीय"
    try:
        pr = max(1, min(5, int(data.get("priority", 3))))
    except Exception:
        pr = 3
    tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()][:5]
    slug = re.sub(r"[^a-z0-9]+", "-", str(data.get("slug", "")).lower()).strip("-")[:60] or "khabar"
    slug = f"{slug}-{hashlib.md5(item['id'].encode()).hexdigest()[:5]}"
    pub = item["published"] or datetime.now(timezone.utc)
    return {
        "slug": slug,
        "headline": headline,
        "summary": summary,
        "body": body,
        "category": cat,
        "priority": pr,
        "tags": tags,
        "seo_description": str(data.get("seo_description", "")).strip() or summary,
        "story_key": re.sub(r"[^a-z0-9-]+", "-", str(data.get("story_key", "")).lower()).strip("-"),
        "published": pub.astimezone(IST).isoformat(),
        "sources": [{"name": item["source"], "url": item["url"]}],
    }


# ---------------------------------------------------------------- rendering

CSS = """
:root{--sf:#E8590C;--ink:#15181c;--mut:#6b7280;--line:#ececec;--bg:#fff;--card:#fafafa}
@media (prefers-color-scheme:dark){:root{--ink:#eceef1;--mut:#9aa3af;--line:#272b31;--bg:#0f1114;--card:#171a1f}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:'Noto Sans Devanagari',system-ui,-apple-system,'Segoe UI',sans-serif;line-height:1.75;font-size:17px}
a{color:inherit;text-decoration:none}
.wrap{max-width:1040px;margin:0 auto;padding:0 16px}
header.top{border-bottom:1px solid var(--line);background:var(--bg);position:sticky;top:0;z-index:5}
.brand{display:flex;align-items:baseline;gap:12px;padding:10px 0 2px;flex-wrap:wrap}
.logo{font-size:30px;font-weight:800;line-height:1.3}
.logo i{color:var(--sf);font-style:normal}
.tag{color:var(--mut);font-size:13px}
nav{display:flex;gap:4px;overflow-x:auto;padding:4px 0 8px;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav a{white-space:nowrap;padding:3px 14px;border-radius:999px;font-size:15px;color:var(--mut)}
nav a.on,nav a:hover{color:#fff;background:var(--sf)}
main{padding:20px 0 40px}
.cat{color:var(--sf);font-size:13px;font-weight:700}
.meta{color:var(--mut);font-size:13px}
.hero{display:block;border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:22px}
.hero h2{font-size:clamp(26px,5vw,38px);line-height:1.4;margin:6px 0 8px;font-weight:800}
.hero p{margin:0 0 8px;color:var(--mut);font-size:18px}
h3.sec{font-size:15px;color:var(--mut);margin:0 0 12px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px}
.card{display:block;border:1px solid var(--line);background:var(--card);border-radius:12px;padding:14px 16px}
.card:hover{border-color:var(--sf)}
.card h2{font-size:19px;line-height:1.5;margin:4px 0 6px;font-weight:700}
.card p{margin:0 0 6px;color:var(--mut);font-size:15px;line-height:1.65}
.art{max-width:720px;margin:0 auto}
.art h1{font-size:clamp(25px,5vw,34px);line-height:1.45;margin:8px 0 10px;font-weight:800}
.deck{font-size:19px;color:var(--mut);margin:0 0 14px}
.art .txt p{font-size:18px;margin:0 0 18px}
.src{border-left:3px solid var(--sf);background:var(--card);padding:10px 14px;margin:22px 0;font-size:15px;border-radius:0 8px 8px 0}
.src a{color:var(--sf);font-weight:600}
.share{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}
.share a{border:1px solid var(--line);border-radius:999px;padding:4px 16px;font-size:15px}
.share a:hover{border-color:var(--sf);color:var(--sf)}
.tags{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}
.tags span{font-size:13px;color:var(--mut);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:0 8px}
.empty{text-align:center;color:var(--mut);padding:60px 0}
footer{border-top:1px solid var(--line);color:var(--mut);font-size:13px;padding:18px 0 30px;line-height:1.7}
footer a{color:var(--sf)}
.prose p,.prose li{font-size:17px}
.card img,.hero img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;display:block;background:var(--line);margin-bottom:10px}
.hero img{max-height:440px;border-radius:14px}
.art figure{margin:14px 0}
.art figure img{width:100%;border-radius:12px;display:block;background:var(--line)}
figcaption{font-size:13px;color:var(--mut);margin-top:4px}
"""


def esc(s):
    return html.escape(str(s), quote=True)


def fmt_date(iso):
    d = datetime.fromisoformat(iso).astimezone(IST)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}, {d.strftime('%H:%M')}"


def nav_html(prefix, active=None):
    links = [f'<a href="{prefix}index.html" class="{"on" if active == "home" else ""}">मुख्य पृष्ठ</a>']
    for name, slug in CATEGORIES.items():
        links.append(f'<a href="{prefix}category/{slug}.html" class="{"on" if active == name else ""}">{esc(name)}</a>')
    return "<nav>" + "".join(links) + "</nav>"


def page(title, desc, main, prefix, canonical, active=None, og="website", head_extra=""):
    year = datetime.now(IST).year
    return f"""<!doctype html>
<html lang="hi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(canonical)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="{og}">
<meta property="og:url" content="{esc(canonical)}">
<meta property="og:site_name" content="{SITE_NAME}">
<meta name="theme-color" content="#E8590C">
<link rel="alternate" type="application/rss+xml" title="{SITE_NAME}" href="{SITE_URL}/feed.xml">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23E8590C'/%3E%3Ctext x='32' y='46' font-size='40' font-weight='800' text-anchor='middle' fill='white' font-family='sans-serif'%3E%E0%A4%A4%3C/text%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Devanagari:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>{CSS}</style>
{head_extra}
</head>
<body>
<header class="top"><div class="wrap">
<div class="brand"><a class="logo" href="{prefix}index.html">तथ्य<i>.</i></a><span class="tag">{TAGLINE}</span></div>
{nav_html(prefix, active)}
</div></header>
<main class="wrap">
{main}
</main>
<footer><div class="wrap">
© {year} तथ्य · <a href="{prefix}about.html">हमारे बारे में</a> · <a href="{prefix}feed.xml">RSS</a><br>
ख़बरें चुने हुए सार्वजनिक स्रोतों के आधार पर AI की मदद से हिंदी में तैयार की जाती हैं। हर ख़बर के साथ मूल स्रोत का लिंक दिया गया है। पुष्टि के लिए मूल स्रोत देखें।
</div></footer>
</body>
</html>
"""


def story_url(s, prefix=""):
    return f"{prefix}news/{s['slug']}.html"


def img_tag(s, prefix):
    if not s.get("image"):
        return ""
    return (f'<img src="{prefix}{esc(s["image"])}" alt="{esc(s["headline"])}" '
            f'loading="lazy" referrerpolicy="no-referrer">')


def card(s, prefix):
    return (f'<a class="card" href="{story_url(s, prefix)}">{img_tag(s, prefix)}'
            f'<span class="cat">{esc(s["category"])}</span>'
            f'<h2>{esc(s["headline"])}</h2><p>{esc(s["summary"])}</p>'
            f'<span class="meta">{esc(fmt_date(s["published"]))} · {esc(s["sources"][0]["name"])}</span></a>')


def render_home(stories):
    if not stories:
        main = '<div class="empty">ख़बरें जल्द ही यहाँ दिखेंगी।</div>'
    else:
        recent = stories[:20]
        hero = max(recent, key=lambda s: (s["priority"], s["published"]))
        rest = [s for s in stories[:40] if s is not hero]
        main = (f'<a class="hero" href="{story_url(hero)}">{img_tag(hero, "")}<span class="cat">{esc(hero["category"])}</span>'
                f'<h2>{esc(hero["headline"])}</h2><p>{esc(hero["summary"])}</p>'
                f'<span class="meta">{esc(fmt_date(hero["published"]))} · {esc(hero["sources"][0]["name"])}</span></a>'
                f'<h3 class="sec">ताज़ा ख़बरें</h3><div class="grid">{"".join(card(s, "") for s in rest)}</div>')
    return page(f"{SITE_NAME}: {TAGLINE}", TAGLINE + "। राष्ट्रीय, अंतरराष्ट्रीय, व्यापार, टेक और खेल की ताज़ा ख़बरें हिंदी में।",
                main, "", SITE_URL + "/", active="home")


def render_category(cat, slug, stories):
    items = [s for s in stories if s["category"] == cat][:100]
    main = (f'<h3 class="sec">{esc(cat)}</h3>' +
            (f'<div class="grid">{"".join(card(s, "../") for s in items)}</div>' if items
             else '<div class="empty">इस श्रेणी में अभी कोई ख़बर नहीं है।</div>'))
    return page(f"{cat} ख़बरें | {SITE_NAME}", f"{cat} की ताज़ा ख़बरें हिंदी में, {SITE_NAME} पर।",
                main, "../", f"{SITE_URL}/category/{slug}.html", active=cat)


def render_story(s, stories):
    paras = "".join(f"<p>{esc(p.strip())}</p>" for p in s["body"].split("\n\n") if p.strip())
    src = " · ".join(f'<a href="{esc(x["url"])}" rel="noopener nofollow" target="_blank">{esc(x["name"])}</a>' for x in s["sources"])
    url = f"{SITE_URL}/news/{s['slug']}.html"
    share_text = quote(f"{s['headline']}\n{url}")
    related = [r for r in stories if r["category"] == s["category"] and r["slug"] != s["slug"]][:4]
    rel_html = (f'<h3 class="sec" style="margin-top:30px">और पढ़ें</h3><div class="grid">{"".join(card(r, "../") for r in related)}</div>'
                if related else "")
    tags = "".join(f"<span>{esc(t)}</span>" for t in s["tags"])
    fig = ""
    og_img = ""
    if s.get("image"):
        fig = (f'<figure>{img_tag(s, "../")}<figcaption>फ़ोटो: {esc(s.get("image_credit", ""))}</figcaption></figure>')
        og_img = (f'<meta property="og:image" content="{esc(SITE_URL + "/" + s["image"])}">'
                  f'<meta name="twitter:card" content="summary_large_image">')
    ld = json.dumps({
        "@context": "https://schema.org", "@type": "NewsArticle",
        "headline": s["headline"], "description": s["seo_description"],
        "datePublished": s["published"], "inLanguage": "hi",
        "mainEntityOfPage": url,
        "publisher": {"@type": "Organization", "name": SITE_NAME},
    }, ensure_ascii=False)
    main = (f'<article class="art"><span class="cat">{esc(s["category"])}</span>'
            f'<h1>{esc(s["headline"])}</h1><p class="deck">{esc(s["summary"])}</p>'
            f'<div class="meta">{esc(fmt_date(s["published"]))}</div>'
            f'{fig}'
            f'<div class="txt" style="margin-top:18px">{paras}</div>'
            f'<div class="src">स्रोत: {src}<br><span class="meta">मूल रिपोर्ट के लिए स्रोत का लिंक खोलें।</span></div>'
            f'<div class="tags">{tags}</div>'
            f'<div class="share"><a href="https://wa.me/?text={share_text}" target="_blank" rel="noopener">WhatsApp पर भेजें</a>'
            f'<a href="https://t.me/share/url?url={quote(url)}&text={quote(s["headline"])}" target="_blank" rel="noopener">Telegram पर भेजें</a></div>'
            f'{rel_html}</article>')
    return page(f"{s['headline']} | {SITE_NAME}", s["seo_description"], main, "../", url,
                active=s["category"], og="article",
                head_extra=f'{og_img}<script type="application/ld+json">{ld}</script>')


def render_about(cfg):
    names = [c["name"] for c in cfg.get("telegram", [])] + [r["name"] for r in cfg.get("rss", [])]
    main = f"""<div class="art prose">
<h1>हमारे बारे में</h1>
<p><b>तथ्य</b> एक हिंदी समाचार मंच है। हमारा लक्ष्य है देश और दुनिया की ख़बरें सरल, सटीक और बिना शोर के पाठकों तक पहुँचाना।</p>
<h3>हम कैसे काम करते हैं</h3>
<p>हम सार्वजनिक रूप से उपलब्ध समाचार स्रोतों से ख़बरें चुनते हैं और AI की मदद से उन्हें हिंदी में नए सिरे से लिखते हैं। हर ख़बर के साथ मूल स्रोत का नाम और लिंक दिया जाता है। हम अफ़वाह, विज्ञापन और अपुष्ट दावों को शामिल नहीं करने की कोशिश करते हैं।</p>
<h3>हमारे स्रोत</h3>
<p>{esc(", ".join(names))}</p>
<h3>सुधार नीति</h3>
<p>स्वचालित प्रक्रिया में चूक संभव है। किसी ख़बर में गलती दिखे तो मूल स्रोत से मिलान करें। पुष्टि के लिए हमेशा मूल स्रोत को प्राथमिकता दें।</p>
</div>"""
    return page(f"हमारे बारे में | {SITE_NAME}", "तथ्य के बारे में: हम कैसे ख़बरें चुनते और लिखते हैं।", main, "", f"{SITE_URL}/about.html")


def render_feed(stories):
    items = []
    for s in stories[:30]:
        url = f"{SITE_URL}/news/{s['slug']}.html"
        pub = email.utils.format_datetime(datetime.fromisoformat(s["published"]))
        items.append(f"<item><title>{esc(s['headline'])}</title><link>{url}</link><guid>{url}</guid>"
                     f"<pubDate>{pub}</pubDate><category>{esc(s['category'])}</category>"
                     f"<description>{esc(s['summary'])}</description></item>")
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<title>{SITE_NAME}</title><link>{SITE_URL}/</link><description>{esc(TAGLINE)}</description>"
            f"<language>hi</language>{''.join(items)}</channel></rss>")


def render_sitemap(stories):
    urls = [(f"{SITE_URL}/", None), (f"{SITE_URL}/about.html", None)]
    urls += [(f"{SITE_URL}/category/{slug}.html", None) for slug in CATEGORIES.values()]
    urls += [(f"{SITE_URL}/news/{s['slug']}.html", s["published"]) for s in stories]
    rows = "".join(f"<url><loc>{esc(u)}</loc>" + (f"<lastmod>{esc(d)}</lastmod>" if d else "") + "</url>" for u, d in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_site(stories, out, cfg):
    stories = sorted(stories, key=lambda s: s["published"], reverse=True)
    write(out / "index.html", render_home(stories))
    write(out / "about.html", render_about(cfg))
    for cat, slug in CATEGORIES.items():
        write(out / "category" / f"{slug}.html", render_category(cat, slug, stories))
    keep = set()
    for s in stories:
        write(out / "news" / f"{s['slug']}.html", render_story(s, stories))
        keep.add(f"{s['slug']}.html")
    news_dir = out / "news"
    if news_dir.exists():
        for f in news_dir.glob("*.html"):
            if f.name not in keep:
                f.unlink()
    used = {Path(s["image"]).name for s in stories if s.get("image")}
    img_dir = out / "images"
    if img_dir.exists():
        for f in img_dir.iterdir():
            if f.is_file() and f.name not in used:
                f.unlink()
    write(out / "feed.xml", render_feed(stories))
    write(out / "sitemap.xml", render_sitemap(stories))
    write(out / "robots.txt", f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}/sitemap.xml\n")
    write(out / ".nojekyll", "")
    log(f"Built site with {len(stories)} stories in {out}")


# --------------------------------------------------------------------- main

def sample_stories():
    now = datetime.now(IST)
    def mk(i, cat, pr):
        return {
            "slug": f"namuna-khabar-{i}", "headline": f"नमूना ख़बर {i}: यह केवल डिज़ाइन देखने के लिए लिखा गया शीर्षक है",
            "summary": "यह नमूना सारांश है। असली ख़बरें स्वचालित प्रक्रिया से आएँगी और हर ख़बर के साथ मूल स्रोत का लिंक होगा।",
            "body": "यह नमूना ख़बर का पहला पैराग्राफ है।\n\nयह दूसरा पैराग्राफ है, जो पेज की बनावट दिखाने के लिए रखा गया है।",
            "category": cat, "priority": pr, "tags": ["नमूना", "डिज़ाइन"],
            "seo_description": "डिज़ाइन जाँचने के लिए नमूना ख़बर।", "story_key": f"demo-{i}",
            "published": (now - timedelta(hours=i)).isoformat(),
            "sources": [{"name": "नमूना स्रोत", "url": "https://example.com"}],
        }
    cats = list(CATEGORIES)
    return [mk(i, cats[i % len(cats)], 5 if i == 0 else 3) for i in range(8)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    if args.demo:
        build_site(sample_stories(), ROOT / "preview", cfg)
        return

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    stories_path, seen_path = data_dir / "stories.json", data_dir / "seen.json"
    stories = json.loads(stories_path.read_text(encoding="utf-8")) if stories_path.exists() else []
    seen_list = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else []
    seen = set(seen_list)
    prompt_tpl = (ROOT / "tathya_editor_prompt.md").read_text(encoding="utf-8")

    candidates = collect(cfg, seen)
    log(f"{len(candidates)} candidate items")
    calls, added = 0, 0
    for item in candidates:
        if calls >= MAX_NEW_PER_RUN:
            break
        log(f"-> {item['source']}: {item['url']}")
        try:
            calls += 1
            data = gemini_json(prompt_tpl.replace("{{SOURCE_TEXT}}", build_source_text(item)))
            if isinstance(data, dict) and data.get("skip"):
                log("   skipped:", data.get("reason"))
                seen_list.append(item["id"])
            else:
                story = make_story(data, item)
                cutoff = datetime.now(IST) - timedelta(hours=DEDUP_HOURS)
                dup = next((x for x in stories if story["story_key"] and x.get("story_key") == story["story_key"]
                            and datetime.fromisoformat(x["published"]) > cutoff), None)
                if dup:
                    if not any(s["url"] == item["url"] for s in dup["sources"]):
                        dup["sources"].append({"name": item["source"], "url": item["url"]})
                    log("   duplicate of", dup["slug"])
                else:
                    if item.get("show_images"):
                        img = item.get("image")
                        if not img and item["kind"] == "rss":
                            try:
                                img = fetch_og_image(item["url"])
                            except Exception:
                                img = None
                        if img:
                            path = download_image(urljoin(item["url"], html.unescape(img)), item["id"])
                            if path:
                                story["image"] = path
                                story["image_credit"] = item["source"]
                    stories.append(story)
                    added += 1
                    log("   added:", story["headline"])
                seen_list.append(item["id"])
        except QuotaError:
            log("Gemini quota reached, stopping this run.")
            break
        except ValueError as e:
            log("   bad output, marking as seen:", e)
            seen_list.append(item["id"])
        except Exception as e:
            log("   error (will retry next run):", e)
        time.sleep(GEMINI_PAUSE)

    stories.sort(key=lambda s: s["published"], reverse=True)
    stories = stories[:MAX_STORIES]
    write(stories_path, json.dumps(stories, ensure_ascii=False, indent=1))
    write(seen_path, json.dumps(seen_list[-4000:], ensure_ascii=False))
    build_site(stories, ROOT, cfg)
    log(f"Done. New stories this run: {added}")


if __name__ == "__main__":
    main()
