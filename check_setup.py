"""
Transreport AI Intern case study - setup checker.

Tests every item on the Stage 1 checklist and prints a status table
you can paste into your reply email.

Usage:
    pip install -r requirements.txt
    cp .env.example .env     # then fill in your keys
    python check_setup.py
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TIMEOUT = 20
results = []  # (item, status, note)


def record(item, status, note=""):
    results.append((item, status, note))
    mark = {"Working": "[ok]", "Not working": "[!!]", "Skipped": "[--]"}[status]
    print(f"{mark} {item}: {status}" + (f" - {note}" if note else ""))


# --------------------------------------------------------------------------
# 1. Language model: Gemini (preferred) or Groq (fallback)
# --------------------------------------------------------------------------
def check_gemini():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None, "no GEMINI_API_KEY in .env"
    try:
        from google import genai
    except ImportError:
        return False, "google-genai not installed"
    try:
        client = genai.Client(api_key=key)
    except Exception as e:
        return False, f"client init failed: {type(e).__name__}: {str(e)[:100]}"

    # Ask the account which models it can actually use, rather than hardcoding
    # a name - Google retires model names for new keys regularly.
    try:
        usable = []
        for m in client.models.list():
            actions = (getattr(m, "supported_actions", None)
                       or getattr(m, "supported_generation_methods", None)
                       or [])
            if "generateContent" in actions:
                usable.append(m.name)
    except Exception as e:
        return False, f"could not list models: {type(e).__name__}: {str(e)[:100]}"

    if not usable:
        return False, "key works but no models support generateContent"

    print(f"    available Gemini models: {', '.join(n.split('/')[-1] for n in usable[:12])}")

    # Prefer small/fast models, and skip ones that cost or need extra setup.
    def rank(name):
        n = name.lower()
        if "embedding" in n or "aqa" in n or "image" in n or "tts" in n:
            return 99
        if "flash-lite" in n:
            return 0
        if "flash" in n:
            return 1
        if "pro" in n:
            return 2
        return 3

    errors = []
    for name in sorted(usable, key=rank)[:4]:
        if rank(name) == 99:
            continue
        try:
            resp = client.models.generate_content(
                model=name,
                contents="Reply with the single word: ready",
            )
            short = name.split("/")[-1]
            return True, f"{short} replied: {(resp.text or '').strip()[:40]}"
        except Exception as e:
            errors.append(f"{name.split('/')[-1]}: {type(e).__name__}")
    return False, f"no model accepted a call ({'; '.join(errors)})"


def check_groq():
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None, "no GROQ_API_KEY in .env"
    try:
        from groq import Groq
    except ImportError:
        return False, "groq not installed"
    try:
        client = Groq(api_key=key)
        # Ask Groq which models it currently serves, so this doesn't break
        # when they retire a model name.
        models = [m.id for m in client.models.list().data]
        chat_models = [m for m in models if "whisper" not in m and "guard" not in m]
        if not chat_models:
            return False, "no chat models returned"
        model = chat_models[0]
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            max_tokens=10,
        )
        return True, f"{model} replied: {resp.choices[0].message.content.strip()[:40]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def check_language_model():
    notes = []
    for name, fn in (("Gemini", check_gemini), ("Groq", check_groq)):
        ok, note = fn()
        if ok:
            record("Language model (Gemini or Groq)", "Working", f"{name} - {note}")
            return
        if ok is False:
            notes.append(f"{name} failed ({note})")
        else:
            notes.append(f"{name} not configured")
    record("Language model (Gemini or Groq)", "Not working", "; ".join(notes))


# --------------------------------------------------------------------------
# 2. Web search: Tavily (preferred) or Exa (fallback)
# --------------------------------------------------------------------------
def check_tavily():
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return None, "no TAVILY_API_KEY in .env"
    try:
        from tavily import TavilyClient
    except ImportError:
        return False, "tavily-python not installed"
    try:
        client = TavilyClient(api_key=key)
        resp = client.search("UK rail passenger assistance", max_results=3)
        n = len(resp.get("results", []))
        return True, f"test search returned {n} results"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def check_exa():
    key = os.getenv("EXA_API_KEY")
    if not key:
        return None, "no EXA_API_KEY in .env"
    try:
        r = requests.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": key, "Content-Type": "application/json"},
            json={"query": "UK rail passenger assistance", "numResults": 3},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return True, f"test search returned {len(r.json().get('results', []))} results"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def check_web_search():
    notes = []
    for name, fn in (("Tavily", check_tavily), ("Exa", check_exa)):
        ok, note = fn()
        if ok:
            record("Web search (Tavily or Exa)", "Working", f"{name} - {note}")
            return
        if ok is False:
            notes.append(f"{name} failed ({note})")
        else:
            notes.append(f"{name} not configured")
    record("Web search (Tavily or Exa)", "Not working", "; ".join(notes))


# --------------------------------------------------------------------------
# 3. GitHub - checks the account exists and (optionally) that a token works
# --------------------------------------------------------------------------
def check_github():
    user = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_TOKEN")
    if token:
        try:
            r = requests.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            record("GitHub", "Working", f"authenticated as {r.json()['login']}")
            return
        except Exception as e:
            record("GitHub", "Not working", f"token rejected: {str(e)[:100]}")
            return
    if not user:
        record("GitHub", "Skipped", "set GITHUB_USERNAME in .env to check")
        return
    try:
        r = requests.get(f"https://api.github.com/users/{user}", timeout=TIMEOUT)
        r.raise_for_status()
        record("GitHub", "Working", f"account {user} exists")
    except Exception as e:
        record("GitHub", "Not working", f"{type(e).__name__}: {str(e)[:100]}")


# --------------------------------------------------------------------------
# 4. Bluesky - public search, no key needed
# --------------------------------------------------------------------------
def check_bluesky():
    # searchPosts is served by api.bsky.app. The public.api.bsky.app host
    # answers search with a CDN 403, even though its other endpoints are open.
    hosts = ["https://api.bsky.app", "https://public.api.bsky.app"]
    errors = []
    for host in hosts:
        try:
            r = requests.get(
                f"{host}/xrpc/app.bsky.feed.searchPosts",
                params={"q": "train delay", "limit": 5, "sort": "latest"},
                headers={"User-Agent": "transreport-setup-check/1.0"},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            posts = r.json().get("posts", [])
            record("Bluesky", "Working",
                   f"{len(posts)} posts from {host}, no auth "
                   f"(note: logged-out search is 1 page, no cursor)")
            return
        except Exception as e:
            errors.append(f"{host} -> {type(e).__name__} {str(e)[:60]}")
    record("Bluesky", "Not working", "; ".join(errors))


# --------------------------------------------------------------------------
# 5. YouTube Data API v3
# --------------------------------------------------------------------------
def check_youtube():
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        record("YouTube Data API", "Skipped", "no YOUTUBE_API_KEY in .env")
        return
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": "passenger assistance train",
                "maxResults": 3,
                "type": "video",
                "key": key,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        record("YouTube Data API", "Working", f"search returned {len(items)} videos")
    except requests.HTTPError as e:
        body = e.response.text[:150] if e.response is not None else ""
        record("YouTube Data API", "Not working", f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        record("YouTube Data API", "Not working", f"{type(e).__name__}: {str(e)[:100]}")


# --------------------------------------------------------------------------
# 6. Hacker News via Algolia - no key needed
# --------------------------------------------------------------------------
def check_hackernews():
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": "accessibility", "hitsPerPage": 5},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
        record("Hacker News (Algolia)", "Working", f"search returned {len(hits)} hits, no auth")
    except Exception as e:
        record("Hacker News (Algolia)", "Not working", f"{type(e).__name__}: {str(e)[:100]}")


# --------------------------------------------------------------------------
# 7. App store reviews (optional) - Apple RSS feed
# --------------------------------------------------------------------------
def check_app_store():
    if os.getenv("SKIP_APP_STORE", "").lower() in ("1", "true", "yes"):
        record("App store reviews (optional)", "Skipped", "skipped by choice")
        return

    app_id = os.getenv("APPLE_APP_ID")
    if not app_id:
        record("App store reviews (optional)", "Skipped",
               "set APPLE_APP_ID in .env to test a specific app")
        return

    tried = []
    for store in ("gb", "us"):
        url = (f"https://itunes.apple.com/{store}/rss/customerreviews/"
               f"id={app_id}/sortby=mostrecent/json")
        try:
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            entries = r.json().get("feed", {}).get("entry", [])
            # Apple's feed puts app metadata first; real reviews have an author.
            reviews = [e for e in entries if "author" in e and "content" in e]
            if reviews:
                record("App store reviews (optional)", "Working",
                       f"Apple RSS: {len(reviews)} reviews from {store} store; "
                       f"no official free Google Play route")
                return
            tried.append(f"{store}: 0 reviews")
        except Exception as e:
            tried.append(f"{store}: {type(e).__name__}")
    record("App store reviews (optional)", "Not working",
           f"Apple RSS patchy ({'; '.join(tried)}); no official free Google Play route")


# --------------------------------------------------------------------------
# 8. Tracing (optional) - Langfuse or LangSmith
# --------------------------------------------------------------------------
def check_tracing():
    if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
        try:
            r = requests.get(
                f"{host}/api/public/projects",
                auth=(os.getenv("LANGFUSE_PUBLIC_KEY"), os.getenv("LANGFUSE_SECRET_KEY")),
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            record("Tracing tool (optional)", "Working", "Langfuse keys authenticate")
            return
        except Exception as e:
            record("Tracing tool (optional)", "Not working", f"Langfuse: {str(e)[:100]}")
            return

    if os.getenv("LANGSMITH_API_KEY"):
        try:
            r = requests.get(
                "https://api.smith.langchain.com/info",
                headers={"x-api-key": os.getenv("LANGSMITH_API_KEY")},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            record("Tracing tool (optional)", "Working", "LangSmith key authenticates")
            return
        except Exception as e:
            record("Tracing tool (optional)", "Not working", f"LangSmith: {str(e)[:100]}")
            return

    record("Tracing tool (optional)", "Skipped", "no tracing keys in .env")


# --------------------------------------------------------------------------
def print_table():
    print("\n" + "=" * 78)
    print("PASTE THIS INTO YOUR REPLY")
    print("=" * 78 + "\n")

    w_item = max(len(r[0]) for r in results) + 2
    w_status = 13
    print(f"| {'Item'.ljust(w_item)} | {'Status'.ljust(w_status)} | Notes")
    print(f"|{'-' * (w_item + 2)}|{'-' * (w_status + 2)}|{'-' * 40}")
    for item, status, note in results:
        print(f"| {item.ljust(w_item)} | {status.ljust(w_status)} | {note}")

    core = [r for r in results if r[0].startswith(("Language model", "Web search", "GitHub"))]
    sources = [r for r in results if r[0] in ("Bluesky", "YouTube Data API", "Hacker News (Algolia)")]
    core_ok = all(r[1] == "Working" for r in core)
    sources_ok = sum(1 for r in sources if r[1] == "Working")

    print("\n" + "-" * 78)
    print(f"Core access (model + search + GitHub): {'ALL WORKING' if core_ok else 'INCOMPLETE'}")
    print(f"Sources working: {sources_ok} of 3 (you need at least 1)")
    if core_ok and sources_ok >= 1:
        print("\nYou are ready for the case study.")
    else:
        print("\nNot ready yet - see the failures above.")


if __name__ == "__main__":
    print("Transreport setup check\n" + "-" * 78)
    check_language_model()
    check_web_search()
    check_github()
    check_bluesky()
    check_youtube()
    check_hackernews()
    check_app_store()
    check_tracing()
    print_table()
    sys.exit(0)
