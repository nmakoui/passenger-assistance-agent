"""
Passenger Assistance Agent - decision logic.

Three decisions, matching diagram.md:

  1. RELEVANT?      one LLM call -> relevant, category, severity, reason
  2. NEED CONTEXT?  does the post name a station, operator or policy?
                    if yes, one Tavily search
  3. DRAFT?         skip / escalate / draft, based on severity and category

Nothing is ever posted. Every draft is marked for human review.
"""

from __future__ import annotations

import json
import os
import re
import logging
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

from dotenv import load_dotenv

load_dotenv()

TRIAGE_MODEL = "gemini-flash-lite-latest"   # aliases roll forward, never pin
DRAFT_MODEL = "gemini-flash-lite-latest"

# ------------------------------------------------------- tracing (optional) -
try:
    from langfuse import observe
except ImportError:
    def observe(*a, **k):
        def deco(fn):
            return fn
        return deco if not a else a[0]


# ------------------------------------------------------------------- LLM ----
_client = None


def client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", "").strip())
    return _client


def llm_json(prompt, system, model=TRIAGE_MODEL, temperature=0.2, retries=3):
    """One LLM call that must return JSON.

    Retries with a backoff on 503, which gemini-flash-latest returns under
    load, and falls back to the lite model on the last attempt.
    """
    import time
    from google.genai import types

    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        temperature=temperature,
    )
    for attempt in range(1, retries + 1):
        use_model = model if attempt < retries else TRIAGE_MODEL
        try:
            resp = client().models.generate_content(
                model=use_model, contents=prompt, config=cfg)
            raw = (resp.text or "").strip()
            raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
            return json.loads(raw)
        except Exception as e:
            if attempt == retries:
                print(f"    [llm] failed after {retries} tries: "
                      f"{type(e).__name__}: {str(e)[:100]}")
                return {}
            wait = 2 ** attempt
            print(f"    [llm] {type(e).__name__}, retrying in {wait}s")
            time.sleep(wait)
    return {}
# --------------------------------------------------- 0. cheap gate, no LLM --
# Kills obvious noise before spending a call on it. Mostly US transit alerts
# and posts that merely contain the word "assistance".

KEYWORDS = [
    "passenger assist", "assisted travel", "assistance", "wheelchair",
    "ramp", "mobility", "disabled", "disability", "accessible", "step-free",
    "guide dog", "boarding", "turn up and go",
]
RAIL_WORDS = [
    "train", "rail", "station", "platform", "carriage", "conductor",
    "passenger assist", "lner", "avanti", "northern", "scotrail", "gwr",
    "southeastern", "southern", "thameslink", "east midlands", "network rail",
    "transpennine", "crosscountry", "greater anglia", "south western",
]
# US transit accounts flood the results and are out of scope.
NOT_UK = [
    "njtransit", "nj transit", "northeast corridor", "amtrak", "mta",
    "metro-north", "lirr", "septa", "bart", "caltrain", "psny",
]


def prefilter(rec):
    """No LLM. Returns (keep: bool, reason: str)."""
    text = rec["text"].lower()
    if len(text) < 30:
        return False, "too short to judge"
    if any(w in text for w in NOT_UK):
        return False, "not UK rail"
    if not any(k in text for k in KEYWORDS):
        return False, "no assistance keyword"
    if not any(w in text for w in RAIL_WORDS):
        return False, "no rail context"
    return True, "passed keyword gate"


# ------------------------------------------------------------ 1. RELEVANT? --
TRIAGE_SYSTEM = """You triage public posts for a UK rail passenger assistance team.

RELEVANT means: someone describing their own, or a companion's, real experience
of assistance for rail travel in Great Britain. Booking assistance, being met at
a station, ramps, staff not turning up, boarding refused, the Passenger
Assistance app, step-free access, guide dogs, or similar.

NOT RELEVANT: news reports, policy debate with no personal experience, fiction or
book plots, marketing, aviation or bus or non-UK journeys, generic delay
complaints with no assistance element, and automated service alerts.

Return JSON only, exactly these keys:
  relevant   true or false
  category   one of: booking, staff_no_show, boarding_refused, app_issue,
             accessibility, praise, other
  severity   one of: low, medium, high
             high = stranded, injured, left on a train, refused boarding, or a
             missed connection with no help offered
  reason     one sentence, plain English, why you judged it that way
"""


@observe(name="triage")
def triage(rec):
    prompt = (f"Source: {rec['source']}\n"
              f"Posted: {rec['created_at'][:10]}\n"
              f"Post:\n\"\"\"\n{rec['text'][:1500]}\n\"\"\"")
    out = llm_json(prompt, TRIAGE_SYSTEM, TRIAGE_MODEL)
    return {
        "relevant": bool(out.get("relevant")),
        "category": out.get("category", "other"),
        "severity": out.get("severity", "low"),
        "reason": out.get("reason", "no reason returned"),
    }


# -------------------------------------------------------- 2. NEED CONTEXT? --
CONTEXT_SYSTEM = """You decide whether a post about UK rail assistance needs a web
lookup before anyone can reply to it well.

It NEEDS context if it names a train operator, a specific station, a named
service or route, or a policy or scheme the replier would have to check.

It does NOT need context if it is a general experience with no named operator,
station, service or policy.

Return JSON only, exactly these keys:
  needs_context  true or false
  query          a short web search query if true, otherwise null
  reason         one short sentence
"""


@observe(name="needs_context")
def needs_context(rec):
    out = llm_json(f"Post:\n\"\"\"\n{rec['text'][:1200]}\n\"\"\"",
                   CONTEXT_SYSTEM, TRIAGE_MODEL)
    return {
        "needs_context": bool(out.get("needs_context")),
        "query": out.get("query"),
        "reason": out.get("reason", ""),
    }


@observe(name="gather_context")
def gather_context(query, max_results=3):
    """One Tavily search. Credits are limited, so this is never looped."""
    from tavily import TavilyClient

    key = os.getenv("TAVILY_API_KEY")
    if not key or not query:
        return []
    try:
        resp = TavilyClient(api_key=key.strip()).search(
            query, max_results=max_results, search_depth="basic")
    except Exception as e:
        print(f"    [context] search failed: {type(e).__name__}")
        return []
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("content") or "")[:400]}
            for r in resp.get("results", [])]


# --------------------------------------------------------------- 3. DRAFT? --
# Rule first, LLM second. Severity and category decide, not vibes.

ESCALATE_WORDS = [
    "sue", "solicitor", "legal action", "suing", "discrimination claim",
    "ombudsman", "injured", "injury", "ambulance", "assault",
    "safeguarding", "left on the train", "stranded overnight",
    "called the police", "refused to let me",
]


def draft_decision(rec, t):
    """Returns (action, reason) where action is skip | escalate | draft.

    Rule first, LLM second. Note the praise guard: a passenger travelling to a
    hospital appointment is not a safeguarding case, and an early version of
    this escalated exactly that post.
    """
    text = rec["text"].lower()

    if t["category"] != "praise" and any(w in text for w in ESCALATE_WORDS):
        hit = next(w for w in ESCALATE_WORDS if w in text)
        return "escalate", f"escalation keyword present ('{hit}')"
    if t["severity"] == "high":
        return "escalate", "high severity, a specialist should see it first"
    if t["category"] == "other":
        return "skip", "too vague to reply to usefully"
    return "draft", f"{t['category']}, {t['severity']} severity"

DRAFT_SYSTEM = """You draft replies for a UK rail passenger assistance team.
A human reviews every draft before it goes anywhere. You never post.

Rules, all mandatory:
- Acknowledge what actually happened to this specific person. No stock openings.
- Never promise compensation, a refund, an investigation outcome, or a deadline.
- Never promise to pass anything on, tell a manager, or take an internal action.
  You can say the feedback is appreciated. You cannot commit the team to doing
  anything.
- Never ask for personal details in public. Offer a private channel or the
  operator's official complaints route instead.
- Never blame the passenger, the staff, or any named individual.
- No corporate filler. Not "we value your feedback", not "sorry for any
  inconvenience caused". Write like a person.
- Under 280 characters for bluesky. Under 120 words elsewhere.
- British English.
- If the post is praise, thank them warmly and briefly. Do not apologise.

Return JSON only, exactly these keys:
  draft      the reply text
  rationale  one sentence on the approach you took
"""


@observe(name="write_draft")
def write_draft(rec, t, context):
    ctx = ""
    if context:
        ctx = ("\n\nBackground from a web search. Use it only if it helps. "
               "Do not quote it:\n")
        ctx += "\n".join(f"- {c['title']}: {c['snippet'][:200]}" for c in context)

    prompt = (f"Platform: {rec['source']}\n"
              f"Category: {t['category']}   Severity: {t['severity']}\n"
              f"Post:\n\"\"\"\n{rec['text'][:1500]}\n\"\"\"{ctx}")
    out = llm_json(prompt, DRAFT_SYSTEM, DRAFT_MODEL, temperature=0.6)
    return {
        "draft": out.get("draft", ""),
        "rationale": out.get("rationale", ""),
    }


# ------------------------------------------------------------ orchestration -
@observe(name="process")
def process(rec):
    """One record through the whole flow. Mirrors diagram.md exactly."""
    steps = []
    result = {"record": rec, "steps": steps, "action": None,
              "triage": None, "context": [], "draft": None}

    keep, why = prefilter(rec)
    steps.append(f"prefilter: {'pass' if keep else 'DROP'} ({why})")
    if not keep:
        result["action"] = "dropped_prefilter"
        return result

    # 1. RELEVANT?
    t = triage(rec)
    result["triage"] = t
    steps.append(f"1. relevant={t['relevant']} "
                 f"[{t['category']}/{t['severity']}] {t['reason']}")
    if not t["relevant"]:
        result["action"] = "not_relevant"
        return result

    # 2. NEED CONTEXT?
    c = needs_context(rec)
    steps.append(f"2. needs_context={c['needs_context']} {c['reason']}")
    if c["needs_context"]:
        result["context"] = gather_context(c["query"])
        steps.append(f"   searched '{(c['query'] or '')[:60]}' "
                     f"-> {len(result['context'])} results")

    # 3. DRAFT?
    action, why = draft_decision(rec, t)
    steps.append(f"3. {action}: {why}")
    if action != "draft":
        result["action"] = action
        return result

    d = write_draft(rec, t, result["context"])
    if not d["draft"]:
        result["action"] = "draft_failed"
        return result

    result["draft"] = d
    result["action"] = "draft"
    return result