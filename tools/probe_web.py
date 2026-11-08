"""实探：哪些主流站点现在真的能用（feed + 正文 + 搜索）。

    python -m tools.probe_web

只看结论：每条输出 状态 / 耗时 / 字节 / 正文长度。不改任何配置。
"""
from __future__ import annotations

import re
import sys
import time

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

FEEDS = [
    ("sspai",   "https://sspai.com/feed"),
    ("ifanr",   "https://www.ifanr.com/feed"),
    ("ithome",  "https://www.ithome.com/rss/"),
    ("solidot", "https://www.solidot.org/index.rss"),
    ("geekpark", "https://www.geekpark.net/rss"),
    ("tmtpost", "https://www.tmtpost.com/rss"),
    ("sina_tech", "https://rss.sina.com.cn/tech/rollnews.xml"),
    ("cnbeta",  "https://www.cnbeta.com.tw/backend.php"),
    ("guokr",   "https://www.guokr.com/rss/"),
    ("sspai_matrix", "https://sspai.com/feed/matrix"),
]

_LINK = re.compile(r"<link>(?:<!\[CDATA\[)?(https?://[^<\]\s]+)", re.I)
_ATOM = re.compile(r'<link[^>]+href="(https?://[^"]+)"', re.I)
_P = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
_DROP = re.compile(r"<(script|style|noscript|svg|iframe|header|footer|nav)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


def get(url, timeout=(6, 10)):
    return requests.get(url, timeout=timeout, headers={"User-Agent": UA})


def decode(r):
    raw = r.content
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def text_of(html):
    body = _DROP.sub(" ", html)
    paras = []
    for p in _P.findall(body):
        t = _TAG.sub("", p)
        t = _WS.sub(" ", t).strip()
        if len(t) >= 24:
            paras.append(t)
    text = "\n".join(paras)
    if len(text) < 200:
        text = _WS.sub(" ", _TAG.sub(" ", body)).strip()
    return text


def probe_feed(name, url):
    t0 = time.time()
    try:
        r = get(url)
        body = decode(r)
        ok = ("<rss" in body[:2000].lower() or "<feed" in body[:2000].lower())
        n = len(_LINK.findall(body)) + len(_ATOM.findall(body))
        print(f"FEED  {name:<10} {'OK ' if ok else 'HTML'} {r.status_code} "
              f"{len(r.content):>7}B {time.time()-t0:5.2f}s  items~{n}")
        if not ok:
            print(f"      head: {body[:120]!r}")
            return None
        items_xml = re.findall(r"<item\b.*?</item>", body, re.S | re.I)
        items_xml += re.findall(r"<entry\b.*?</entry>", body, re.S | re.I)
        if not items_xml:
            return None
        m = re.search(r"<link>\s*(?:<!\[CDATA\[)?(https?://[^<\]\s]+)", items_xml[0], re.I)
        if not m:
            m = re.search(r'<link[^>]+href="(https?://[^"]+)"', items_xml[0], re.I)
        if not m:
            return None
        art = m.group(1)
        mt = re.search(r"<title[^>]*>(.*?)</title>", items_xml[0], re.S | re.I)
        title = _WS.sub(" ", _TAG.sub("", mt.group(1))).strip() if mt else ""
        print(f"      ITEM {title[:34]!r}  {art[:70]}")
        t1 = time.time()
        ar = get(art)
        text = text_of(decode(ar))
        print(f"      ART  {art[:70]}  {ar.status_code} {len(text)}字 "
              f"{time.time()-t1:.2f}s  首句：{text[:60]!r}")
        return art
    except Exception as e:
        print(f"FEED  {name:<10} FAIL {type(e).__name__}: {e}  {time.time()-t0:.2f}s")
        return None


def probe_zhihu():
    t0 = time.time()
    try:
        r = get("https://daily.zhihu.com/api/4/news/latest")
        d = r.json()
        stories = d.get("stories") or []
        print(f"ZHIHU latest  OK {r.status_code} {len(stories)} 条 {time.time()-t0:.2f}s")
        if not stories:
            return
        sid = stories[0]["id"]
        t1 = time.time()
        rr = get(f"https://daily.zhihu.com/api/4/news/{sid}")
        dd = rr.json()
        text = text_of(dd.get("body") or "")
        print(f"      ART  id={sid} {len(text)}字 {time.time()-t1:.2f}s "
              f"title={dd.get('title')!r} 首句：{text[:60]!r}")
    except Exception as e:
        print(f"ZHIHU latest  FAIL {type(e).__name__}: {e}")


def probe_search():
    q = "记忆 是怎么保存的 大脑"
    for name, url, pat in [
        ("bing", "https://www.bing.com/search", r'<li class="b_algo"'),
        ("sogou", "https://www.sogou.com/web", r'<h3[^>]*class="vr-title"'),
        ("baidu", "https://www.baidu.com/s", r'<div[^>]+class="result'),
    ]:
        t0 = time.time()
        try:
            r = get(url, timeout=(6, 10)) if False else requests.get(
                url, params={"q": q} if name != "sogou" else {"query": q},
                timeout=(6, 10), headers={"User-Agent": UA})
            body = decode(r)
            n = len(re.findall(pat, body))
            print(f"SEARCH {name:<6} {r.status_code} {len(r.content):>7}B "
                  f"{time.time()-t0:5.2f}s  结果块 {n}")
            if name == "bing" and n:
                blocks = body.split('<li class="b_algo"')[1:4]
                for b in blocks:
                    m = re.search(r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
                    if m:
                        title = _WS.sub(" ", _TAG.sub("", m.group(2))).strip()
                        print(f"      · {title[:40]}  {m.group(1)[:70]}")
        except Exception as e:
            print(f"SEARCH {name:<6} FAIL {type(e).__name__}: {e}")


if __name__ == "__main__":
    print("== 订阅源 ==")
    arts = []
    for n, u in FEEDS:
        a = probe_feed(n, u)
        if a:
            arts.append((n, a))
    print("\n== 知乎日报 ==")
    probe_zhihu()
    print("\n== 搜索引擎 ==")
    probe_search()
