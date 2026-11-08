"""实探：主流站点在这台机器上到底能不能连、能不能解析出文章。

判定标准只有两条：HTTP 200 且能解析出至少 1 条带标题的条目。
解析不出来就算不可达——能连但给的是反爬页，跟连不上一样没用。
"""
from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from html import unescape

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TAG_RE = re.compile(r"<[^>]+>")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", s or ""))).strip()


def parse_feed(text: str) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return items
    for node in root.iter():
        tag = node.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = link = body = ""
        for child in node:
            name = child.tag.split("}")[-1].lower()
            if name == "title":
                title = clean("".join(child.itertext()))
            elif name == "link":
                link = (child.get("href") or child.text or "").strip()
            elif name in ("description", "summary", "content", "encoded"):
                body = clean("".join(child.itertext()))
        if not link:
            for child in node:
                if child.tag.split("}")[-1].lower() == "guid":
                    link = (child.text or "").strip()
                    break
        items.append({"title": title, "url": link, "len": len(body),
                      "body": body})
        if len(items) >= 3:
            break
    return items


def decode(r: requests.Response) -> str:
    for enc in ("utf-8", "gb18030"):
        try:
            return r.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return r.content.decode("utf-8", "replace")


def get(url: str, timeout: float = 10.0) -> requests.Response:
    return requests.get(url, timeout=timeout, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/json, "
                  "text/xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    })


RSS = [
    ("sspai", "https://sspai.com/feed"),
    ("ifanr", "https://www.ifanr.com/feed"),
    ("ithome", "https://www.ithome.com/rss/"),
    ("solidot", "https://www.solidot.org/index.rss"),
    ("cnbeta", "https://www.cnbeta.com.tw/backend.php"),
    ("guokr", "https://www.guokr.com/rss/"),
    ("36kr", "https://36kr.com/feed"),
    ("ruanyf", "https://www.ruanyifeng.com/blog/atom.xml"),
    ("sina-tech", "https://rss.sina.com.cn/tech/rollnews.xml"),
    ("netease-tech", "https://tech.163.com/special/00097UHL/tech_datalist.xml"),
    ("qq-tech", "https://news.qq.com/rss/tech.xml"),
    ("kexue", "https://www.sciencenet.cn/xml/news.aspx?news=0"),
    ("mittr-cn", "https://www.mittrchina.com/rss"),
    ("huanqiu-kexue", "https://www.huanqiukexue.com/feed"),
    ("pingwest", "https://www.pingwest.com/feed"),
    ("geekpark", "https://www.geekpark.net/rss"),
    ("tmtpost", "https://www.tmtpost.com/rss"),
    ("thepaper", "https://feedx.net/rss/thepaper.xml"),
    ("zaobao", "https://www.zaobao.com.sg/rss/realtime/china"),
    ("ifanr-matrix", "https://www.ifanr.com/feed/matrix"),
]

APIS = [
    ("zhihu-daily", "https://daily.zhihu.com/api/4/news/latest"),
    ("wiki-zh-api", "https://zh.wikipedia.org/w/api.php?action=query&format=json&list=random&rnnamespace=0&rnlimit=3"),
    ("baike-item", "https://baike.baidu.com/item/%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD"),
    ("baidu-search", "https://www.baidu.com/s?wd=%E9%87%8F%E5%AD%90%E8%AE%A1%E7%AE%97"),
    ("bing-search", "https://www.bing.com/search?q=%E9%87%8F%E5%AD%90%E8%AE%A1%E7%AE%97&setlang=zh-CN"),
    ("sogou-search", "https://www.sogou.com/web?query=%E9%87%8F%E5%AD%90%E8%AE%A1%E7%AE%97"),
    ("baidu-news", "https://news.baidu.com/"),
    ("zhihu-hot", "https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=3"),
    ("weibo-hot", "https://weibo.com/ajax/side/hotSearch"),
    ("hackernews", "https://hacker-news.firebaseio.com/v0/topstories.json"),
]


def main() -> int:
    print("=== RSS/Atom ===")
    for name, url in RSS:
        t0 = time.time()
        try:
            r = get(url)
            text = decode(r)
            items = parse_feed(text)
            ok = r.status_code == 200 and bool(items)
            mark = "OK  " if ok else "FAIL"
            sample = items[0]["title"][:34] if items else clean(text)[:40]
            print(f"{mark} {name:<14} {r.status_code} {len(r.content):>7}B "
                  f"{time.time()-t0:5.2f}s items={len(items)} {sample}")
        except Exception as e:
            print(f"FAIL {name:<14} {'':>3} {'':>7}  {time.time()-t0:5.2f}s "
                  f"{type(e).__name__}: {str(e)[:60]}")

    print("\n=== API / 搜索 / 页面 ===")
    for name, url in APIS:
        t0 = time.time()
        try:
            r = get(url)
            ct = r.headers.get("Content-Type", "?")
            text = decode(r)
            looks_json = text.lstrip().startswith(("{", "["))
            print(f"     {name:<14} {r.status_code} {len(r.content):>7}B "
                  f"{time.time()-t0:5.2f}s {ct[:28]} json={looks_json} "
                  f"{clean(text)[:30]}")
        except Exception as e:
            print(f"     {name:<14} {'':>3} {'':>7}  {time.time()-t0:5.2f}s "
                  f"{type(e).__name__}: {str(e)[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
