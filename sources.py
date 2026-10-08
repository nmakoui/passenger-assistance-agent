"""
Source adapters for the Passenger Assistance Agent.

Every fetch_* function returns a list of records with an identical shape, so
the agent never has to know where a comment came from:

    {
        "id":         stable hash, used for dedupe
        "source":     bluesky | appstore | hackernews | web | youtube
        "author":     public handle or display name (no profile data kept)
        "text":       the comment itself, lightly redacted
        "created_at": ISO 8601 string, or "" if the source doesn't give one
        "url":        public permalink a human reviewer can open
        "meta":       small dict, source-specific (rating, query, etc.)
    }

Data care: we keep only the fields above. No avatars, no follower counts, no
emails or phone numbers (stripped on ingest), nothing is written anywhere
except the local output/ folder.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 20
UA = {"User-Agent": "transreport-case-study/0.1 (research prototype)"}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE_RE = re.compile(r"(?:\+44|\b0)\s?\d[\d\s\-]{8,12}\b")


def redact(text: str) -> str:
    """Strip direct contact details before anything is stored or sent to an LLM."""
    text = EMAIL_RE.sub("[email removed]", text or "")
    text = PHONE_RE.sub("[phone removed]", text)
    return " ".join(text.split())


def make_record(source, external_id, author, text, created_at, url, meta=None):
    text = redact(text)
    rid = hashlib.sha1(f"{source}:{external_id}".encode()).hexdigest()[:16]
    return {
        "id": rid,
        "source": source,
        "author": author or "",
        "text": text,
        "created_at": created_at or "",
        "url": url or "",
        "meta": meta or {},
    }


def dedupe(records):
    seen, out = set(), []
    for r in records:
        if r["id"] in seen or not r["text"]:
            continue
        seen.add(r["id"])
        out.append(r)
    return out


# ---------------------------------------------------------------- Bluesky ---
# Public search works unauthenticated, but ONLY on api.bsky.app.
# public.api.bsky.app answers searchPosts with a CDN 403.
# Logged-out search returns a single page; passing a cursor also 403s.

BLUESKY_QUERIES = [
    # the service by name
    "\"passenger assist\"",
    "\"passenger assistance\" train",
    "\"assisted travel\" rail",
    # what people actually say when it fails
    "\"no one met me\" train",
    "\"nobody came\" train wheelchair",
    "\"left on the train\" wheelchair",
    "\"no ramp\" train platform",
    "\"stranded\" station wheelchair",
    "booked assistance train didn't",
    # the equipment and the people
    "wheelchair train station staff",
    "mobility scooter train uk",
    "guide dog train station",
    "step-free access station",
    # operators, where complaints get tagged
    "avanti wheelchair assistance",
    "lner assistance disabled",
    "northern rail wheelchair",
    "scotrail assistance disabled",
    "gwr passenger assist",
]


def fetch_bluesky(queries=None, limit=25):
    queries = queries or BLUESKY_QUERIES
    out = []
    for q in queries:
        try:
            r = requests.get(
                "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
                params={"q": q, "limit": limit, "sort": "latest", "lang": "en"},
                headers=UA,
                timeout=TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"  [bluesky] '{q}' failed: {type(e).__name__}")
            continue

        for p in r.json().get("posts", []):
            rec = p.get("record", {}) or {}
            handle = (p.get("author") or {}).get("handle", "")
            uri = p.get("uri", "")
            rkey = uri.rsplit("/", 1)[-1]
            out.append(make_record(
                "bluesky", uri, handle, rec.get("text", ""),
                rec.get("createdAt", ""),
                f"https://bsky.app/profile/{handle}/post/{rkey}",
                {"query": q, "replies": p.get("replyCount", 0)},
            ))
    return dedupe(out)


# -------------------------------------------------------------- App Store ---
# iTunes RSS customer reviews. Patchy, but it is first-party feedback on the
# actual Passenger Assistance app, which makes it the highest-value source.

def fetch_app_store(app_id=None, store="gb", pages=2):
    app_id = app_id or os.getenv("APPLE_APP_ID", "1542190496")
    out = []
    for page in range(1, pages + 1):
        url = (f"https://itunes.apple.com/{store}/rss/customerreviews/"
               f"page={page}/id={app_id}/sortby=mostrecent/json")
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            entries = (r.json().get("feed") or {}).get("entry") or []
        except Exception as e:
            print(f"  [appstore] {store} page {page} failed: {type(e).__name__}")
            continue

        if isinstance(entries, dict):
            entries = [entries]
        for e in entries:
            # The first entry is app metadata; real reviews have a rating.
            if "im:rating" not in e:
                continue
            body = f"{e.get('title', {}).get('label', '')}. {e.get('content', {}).get('label', '')}"
            out.append(make_record(
                "appstore", e["id"]["label"],
                e.get("author", {}).get("name", {}).get("label", ""),
                body, e.get("updated", {}).get("label", ""),
                e.get("author", {}).get("uri", {}).get("label", ""),
                {"rating": e["im:rating"]["label"], "store": store},
            ))
    return dedupe(out)


# ------------------------------------------------------------ Hacker News ---

HN_QUERIES = ["rail accessibility", "wheelchair train travel", "disabled passenger"]


def fetch_hackernews(queries=None, hits=20):
    queries = queries or HN_QUERIES
    out = []
    for q in queries:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": q, "hitsPerPage": hits,
                        "tags": "(story,comment)"},
                headers=UA, timeout=TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"  [hn] '{q}' failed: {type(e).__name__}")
            continue

        for h in r.json().get("hits", []):
            text = h.get("comment_text") or h.get("story_text") or h.get("title") or ""
            out.append(make_record(
                "hackernews", h["objectID"], h.get("author", ""), text,
                h.get("created_at", ""),
                f"https://news.ycombinator.com/item?id={h['objectID']}",
                {"query": q, "points": h.get("points")},
            ))
    return dedupe(out)


# --------------------------------------------------------- Web via Tavily ---
# Tavily is 1,000 credits/month. Keep the query list short and never loop it.

WEB_QUERIES = [
    "railforums passenger assist experience",
    "passenger assistance train station complaint UK",
    "\"passenger assist\" didn't turn up review",
    "trustpilot UK train assisted travel disabled",
]


def fetch_web(queries=None, max_results=5):
    from tavily import TavilyClient

    key = os.getenv("TAVILY_API_KEY")
    if not key:
        print("  [web] no TAVILY_API_KEY, skipping")
        return []

    client = TavilyClient(api_key=key.strip())
    queries = queries or WEB_QUERIES
    out = []
    for q in queries:
        try:
            resp = client.search(q, max_results=max_results, search_depth="basic")
        except Exception as e:
            print(f"  [web] '{q}' failed: {type(e).__name__}")
            continue
        for item in resp.get("results", []):
            out.append(make_record(
                "web", item.get("url", ""), item.get("title", ""),
                item.get("content", ""), item.get("published_date", "") or "",
                item.get("url", ""), {"query": q, "score": item.get("score")},
            ))
    return dedupe(out)


# ---------------------------------------------------------------- YouTube ---
# search = 100 quota units, commentThreads = 1. Daily budget is 10,000.
# One call to this function costs ~103 units. Do not put it in a loop.

YOUTUBE_QUERIES = [
    "passenger assist train uk wheelchair",
    "disabled train travel uk experience",
    "wheelchair accessible rail travel britain",
    "assisted travel train station uk",
]


def fetch_youtube(queries=None, max_videos=8, max_comments=100):
    """Search videos, then pull their comment threads.

    Quota: search costs 100 units each, commentThreads costs 1. The daily cap
    is 10,000, so four searches plus their comment calls is about 450.
    """
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        print("  [youtube] no YOUTUBE_API_KEY, skipping")
        return []
    key = key.strip()
    queries = queries or YOUTUBE_QUERIES

    video_ids = {}
    for q in queries:
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "snippet", "q": q, "maxResults": max_videos,
                        "type": "video", "relevanceLanguage": "en",
                        "regionCode": "GB", "key": key},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"  [youtube] search '{q}' failed: {type(e).__name__}")
            continue
        for item in r.json().get("items", []):
            video_ids[item["id"]["videoId"]] = item["snippet"]["title"]

    print(f"  [youtube] {len(video_ids)} unique videos from {len(queries)} searches")

    out = []
    for vid, title in video_ids.items():
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                params={"part": "snippet", "videoId": vid,
                        "maxResults": min(max_comments, 100),
                        "order": "relevance", "textFormat": "plainText",
                        "key": key},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
        except Exception:
            continue  # comments disabled is common and fine
        for item in r.json().get("items", []):
            s = item["snippet"]["topLevelComment"]["snippet"]
            out.append(make_record(
                "youtube", item["id"],s.get("authorDisplayName", "").lstrip("@"),
                s.get("textOriginal", ""), s.get("publishedAt", ""),
                f"https://www.youtube.com/watch?v={vid}&lc={item['id']}",
                {"video_id": vid, "video_title": title},
            ))
    return dedupe(out)
# ------------------------------------------------------------------ all ----

REGISTRY = {
    "bluesky": fetch_bluesky,
    "appstore": fetch_app_store,
    "hackernews": fetch_hackernews,
    "web": fetch_web,
    "youtube": fetch_youtube,
}


def fetch_all(sources=("bluesky", "appstore")):
    out = []
    for name in sources:
        fn = REGISTRY.get(name)
        if not fn:
            print(f"  unknown source: {name}")
            continue
        print(f"  fetching {name}...")
        got = fn()
        print(f"    {len(got)} records")
        out.extend(got)
    return dedupe(out)


if __name__ == "__main__":
    recs = fetch_all(("bluesky",))
    for r in recs[:5]:
        print(f"\n[{r['source']}] @{r['author']} {r['created_at'][:10]}")
        print(f"  {r['text'][:180]}")
        print(f"  {r['url']}")