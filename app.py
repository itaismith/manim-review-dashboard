"""Maintainer review dashboard: reads GitHub and groups issues/PRs by who they wait on."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

ROOT = Path(__file__).resolve().parent
API = "https://api.github.com"
SESSION_URL = os.environ.get(
    "DASHBOARD_SESSION_URL",
    "https://app.devin.ai/sessions/0d53ee14eb7046fda7999c2fa4e5f121",
)


def load_config() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text())
    env_repos = os.environ.get("DASHBOARD_REPOS")
    if env_repos:
        cfg["repos"] = [r.strip() for r in env_repos.split(",") if r.strip()]
    cfg.setdefault("landed_days", 14)
    cfg.setdefault("cache_seconds", 60)
    cfg.setdefault("maintainers", [])
    return cfg


# ---------- GitHub access ----------

_token_lock = threading.Lock()
_token: tuple[str, float] | None = None


def github_token() -> str:
    """GITHUB_TOKEN if set, else the box's GitHub connection via `gh auth token` (refreshed every 10 min)."""
    global _token
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    with _token_lock:
        if _token and time.time() - _token[1] < 600:
            return _token[0]
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
        tok = out.stdout.strip()
        if not tok:
            raise RuntimeError("no GitHub token: set GITHUB_TOKEN or authenticate gh")
        _token = (tok, time.time())
        return tok


def gh_get(path: str, params: dict | None = None) -> list | dict:
    headers = {
        "Authorization": f"Bearer {github_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{API}{path}"
    params = dict(params or {})
    params.setdefault("per_page", 100)
    results: list = []
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 401:
            global _token
            _token = None
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return data
        results.extend(data)
        url = r.links.get("next", {}).get("url")
        params = None
    return results


# ---------- parsing helpers ----------

VERDICT_RE = re.compile(r"\bverdict\b\s*\**\s*[:\-]\s*\**\s*([A-Za-z][A-Za-z \-']{1,40})", re.I)
LINKED_RE = re.compile(r"\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s*:?\s+#(\d+)", re.I)
MENTION_RE = re.compile(r"(?<![\w/#&])#(\d+)\b")
ORIGIN_RE = re.compile(r"Originally opened by @([A-Za-z0-9-]+)(?: as (\S+))?")
MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\((\S+?)(?:\s+\"[^\"]*\")?\)")
HTML_IMG_RE = re.compile(r"<img[^>]*?src=[\"']([^\"']+)[\"'][^>]*>", re.I)


def normalize_verdict(text: str) -> str:
    t = text.strip().rstrip(".").lower()
    if t.startswith(("approve", "lgtm", "merge", "ship")):
        return "Approve"
    if "change" in t or t.startswith("request"):
        return "Changes requested"
    if t.startswith(("close", "reject", "decline")):
        return "Close"
    return t.split(".")[0].strip().capitalize()


def find_verdict(body: str | None) -> str | None:
    if not body:
        return None
    m = VERDICT_RE.search(body)
    return normalize_verdict(m.group(1)) if m else None


def _jwt_filename(url: str) -> str:
    m = re.search(r"token=([\w-]+)\.([\w-]+)\.", url)
    if not m:
        return ""
    try:
        payload = m.group(2) + "=" * (-len(m.group(2)) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("filename", "")
    except Exception:
        return ""


def find_gif(body: str | None) -> tuple[str, str] | None:
    """Return (url, alt) of the first GIF / before-after image in a markdown body."""
    if not body:
        return None
    imgs = [(u, a) for a, u in MD_IMG_RE.findall(body)] + [(u, "") for u in HTML_IMG_RE.findall(body)]
    for url, alt in imgs:
        if urlparse(url).scheme not in ("http", "https") or url.endswith(".svg") or "devin-review" in url:
            continue
        path = urlparse(url).path.lower()
        name = _jwt_filename(url).lower()
        label = alt.lower()
        if path.endswith(".gif") or name.endswith(".gif") or "before" in label or "after" in label or "gif" in label:
            return url, alt
    return None


def linked_issues(body: str, issue_numbers: set[int]) -> list[int]:
    """Issues named with a closing keyword ("Fixes #2"), known issues first; else the first known issue mentioned."""
    closing = list(dict.fromkeys(int(n) for n in LINKED_RE.findall(body)))
    if closing:
        return sorted(closing, key=lambda n: n not in issue_numbers)
    for n in MENTION_RE.findall(body):
        if int(n) in issue_numbers:
            return [int(n)]
    return []


def parse_time(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return ""


def login_of(entry: dict) -> str:
    """Login of an entry's author; GitHub returns a null user for deleted accounts."""
    return (entry.get("user") or {}).get("login") or "ghost"


def author_of(item: dict) -> tuple[str, str]:
    """(display author, profile url); mirrored items credit the original author."""
    m = ORIGIN_RE.search(item.get("body") or "")
    if m:
        return m.group(1), f"https://github.com/{m.group(1)}"
    login = login_of(item)
    return login, f"https://github.com/{login}"


# ---------- data collection ----------

MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


def is_maintainer(entry: dict, maintainers: set[str]) -> bool:
    return entry.get("author_association") in MAINTAINER_ASSOCIATIONS or login_of(entry) in maintainers


def pr_details(repo: str, pr: dict, maintainers: set[str]) -> dict:
    n = pr["number"]
    reviews = gh_get(f"/repos/{repo}/pulls/{n}/reviews")
    comments = gh_get(f"/repos/{repo}/issues/{n}/comments")
    review_comments = gh_get(f"/repos/{repo}/pulls/{n}/comments")

    events = []  # (time, verdict, body, url, who)
    for r in reviews:
        if not is_maintainer(r, maintainers):
            continue
        v = {"APPROVED": "Approve", "CHANGES_REQUESTED": "Changes requested"}.get(r["state"]) or find_verdict(r.get("body"))
        if v:
            events.append((r.get("submitted_at"), v, r.get("body") or "", r.get("html_url"), login_of(r)))
    for c in comments + review_comments:
        if not is_maintainer(c, maintainers):
            continue
        v = find_verdict(c.get("body"))
        if v:
            events.append((c.get("created_at") or c.get("updated_at"), v, c.get("body") or "", c.get("html_url"), login_of(c)))
    events.sort(key=lambda e: e[0] or "")
    verdict = events[-1] if events else None

    gif = None
    bodies = ([verdict[2]] if verdict else []) + [pr.get("body") or ""] + [r.get("body") or "" for r in reviews] + [c.get("body") or "" for c in comments + review_comments]
    for b in bodies:
        gif = find_gif(b)
        if gif:
            break
    return {"verdict": verdict, "gif": gif}


def collect_repo(repo: str, landed_days: int, maintainers: set[str]) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=landed_days)
    open_items = gh_get(f"/repos/{repo}/issues", {"state": "open"})
    closed_items = gh_get(f"/repos/{repo}/issues", {"state": "closed", "since": since.isoformat()})
    titles = {i["number"]: i["title"] for i in open_items + closed_items}
    issue_numbers = {i["number"] for i in open_items + closed_items if "pull_request" not in i}

    open_prs = [i for i in open_items if "pull_request" in i]
    with ThreadPoolExecutor(max_workers=6) as ex:
        details = dict(zip([p["number"] for p in open_prs], ex.map(lambda p: pr_details(repo, p, maintainers), open_prs)))

    needs_you, in_progress, landed = [], [], []
    linked_by_open_pr = set()

    for p in open_prs:
        d = details[p["number"]]
        linked_all = linked_issues(p.get("body") or "", issue_numbers)
        linked_by_open_pr.update(linked_all)
        who, who_url = author_of(p)
        item = {
            "repo": repo, "kind": "PR", "number": p["number"], "title": p["title"], "url": p["html_url"],
            "author": who, "author_url": who_url, "updated": parse_time(p["updated_at"]),
            "draft": p.get("draft", False),
            "linked": [(n, titles.get(n), f"https://github.com/{repo}/issues/{n}") for n in linked_all],
            "gif": d["gif"],
        }
        if d["verdict"] and not item["draft"]:
            t, v, _, vurl, vwho = d["verdict"]
            item.update(verdict=v, verdict_url=vurl, verdict_by=vwho, verdict_time=parse_time(t))
            needs_you.append(item)
        else:
            in_progress.append(item)

    for i in open_items:
        if "pull_request" in i or i["number"] in linked_by_open_pr:
            continue
        who, who_url = author_of(i)
        in_progress.append({
            "repo": repo, "kind": "Issue", "number": i["number"], "title": i["title"], "url": i["html_url"],
            "author": who, "author_url": who_url, "updated": parse_time(i["updated_at"]),
            "labels": [lbl["name"] for lbl in i.get("labels", [])],
        })

    for i in closed_items:
        closed = parse_time(i.get("closed_at"))
        if not closed or closed < since:
            continue
        is_pr = "pull_request" in i
        if is_pr:
            outcome = "Merged" if i["pull_request"].get("merged_at") else "Closed"
        else:
            outcome = "Closed as not planned" if i.get("state_reason") == "not_planned" else "Closed"
        who, who_url = author_of(i)
        landed.append({
            "repo": repo, "kind": "PR" if is_pr else "Issue", "number": i["number"], "title": i["title"],
            "url": i["html_url"], "author": who, "author_url": who_url, "closed": closed, "outcome": outcome,
        })

    return {"needs_you": needs_you, "in_progress": in_progress, "landed": landed}


_cache_lock = threading.Lock()
_cache: dict = {}


def dashboard_data(force: bool = False) -> dict:
    cfg = load_config()
    with _cache_lock:
        if not force and _cache and time.time() - _cache["at"] < cfg["cache_seconds"]:
            return _cache["data"]
    data = {"needs_you": [], "in_progress": [], "landed": [], "errors": [], "repos": cfg["repos"]}
    for repo in cfg["repos"]:
        try:
            r = collect_repo(repo, cfg["landed_days"], set(cfg["maintainers"]))
            for k in ("needs_you", "in_progress", "landed"):
                data[k].extend(r[k])
        except Exception as e:  # show per-repo failures instead of a blank page
            data["errors"].append(f"{repo}: {e}")
    data["needs_you"].sort(key=lambda x: x["verdict_time"] or x["updated"], reverse=True)
    data["in_progress"].sort(key=lambda x: x["updated"], reverse=True)
    data["landed"].sort(key=lambda x: x["closed"], reverse=True)
    data["landed_days"] = cfg["landed_days"]
    data["fetched"] = datetime.now(timezone.utc)
    with _cache_lock:
        _cache.update(at=time.time(), data=data)
    return data


# ---------- rendering ----------

e = html.escape


def ref(item: dict, multi: bool) -> str:
    prefix = f"{item['repo'].split('/')[-1]}" if multi else ""
    return f"{prefix}#{item['number']}"


def avatar(name: str) -> str:
    """Seeded conic-gradient avatar, as in math-design's Avatar component."""
    v = 2166136261
    for ch in name:
        v = ((v ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return (
        f'<span aria-hidden="true" class="avatar" style="--angle:{v % 360}deg;'
        f'--x:{20 + (v >> 9) % 60}%;--y:{20 + (v >> 17) % 60}%"></span>'
    )


def person(it: dict) -> str:
    return f'<a class="person" href="{e(it["author_url"])}">{avatar(it["author"])}@{e(it["author"])}</a>'


GLYPHS = {
    "needs": "M3 10h3.5l1 2h3l1-2H15M3 10l2-6h8l2 6v5H3z",
    "progress": "M3 3h5v5H3zM10 3h5v5h-5zM3 10h5v5H3zM10 10h5v5h-5z",
    "landed": "M4 9.5 7.5 13 14 5",
    "refresh": "M14.5 9a5.5 5.5 0 1 1-1.6-3.9M14.5 3v3.5H11",
}


def glyph(name: str) -> str:
    return f'<svg aria-hidden="true" class="glyph" viewBox="0 0 18 18"><path d="{GLYPHS[name]}"/></svg>'


LOGO = (
    '<svg aria-hidden="true" viewBox="0 0 64 64"><defs><linearGradient id="tube" x1="0" x2="1">'
    '<stop offset="0" stop-color="#ffd600"/><stop offset=".5" stop-color="#00e8aa"/>'
    '<stop offset="1" stop-color="#1496ff"/></linearGradient></defs>'
    '<path d="M8 46c10 0 16-4 18-14 2-12-10-14-10-4s14 18 24 18 14-4 16-8" fill="none" '
    'stroke="url(#tube)" stroke-width="7" stroke-linecap="round"/>'
    '<circle cx="56" cy="38" r="4.5" fill="#101012"/></svg>'
)


def render_needs_you(items: list, multi: bool) -> str:
    if not items:
        return '<p class="empty">Nothing is waiting on you.</p>'
    out = []
    for it in items:
        linked = "".join(
            f'<span class="card__caption">Fixes <a href="{e(url)}">#{n}</a>{" " + e(title) if title else ""}</span>'
            for n, title, url in it.get("linked") or []
        )
        if it.get("gif"):
            url, alt = it["gif"]
            figure = f'<a class="card__figure" href="{e(url)}" target="_blank"><img src="{e(url)}" alt="{e(alt or "before/after")}" loading="lazy"></a>'
        else:
            figure = '<span class="card__figure card__figure--none">No before/after GIF</span>'
        out.append(f"""
<article class="card">
  <a class="card__cover" href="{e(it['url'])}" aria-label="{e(it['title'])}"></a>
  <span class="card__top"><span class="card__verdict">{e(it['verdict'])}</span>
    <a class="card__label" href="{e(it.get('verdict_url') or it['url'])}">{e(ref(it, multi))} · {e(ago(it.get('verdict_time')))}</a></span>
  {figure}
  <span class="card__footer">
    <a class="card__title" href="{e(it['url'])}">{e(it['title'])}</a>
    {person(it)}
    {linked}
    <span class="card__actions"><a class="pill pill--ink" href="{e(it['url'])}">Open on GitHub</a>
      <a class="pill" href="{e(it['url'])}/files">Files changed</a></span>
  </span>
</article>""")
    return f'<div class="cards">{"".join(out)}</div>'


def render_rows(items: list, multi: bool, landed: bool = False) -> str:
    if not items:
        return '<p class="empty">Nothing here.</p>'
    rows = []
    for it in items:
        if landed:
            status = it["outcome"]
            when = ago(it["closed"])
        else:
            status = "Draft" if it.get("draft") else ""
            when = ago(it["updated"])
        meta = [it["kind"]] + ([status] if status else [])
        if it.get("linked"):
            meta.append("fixes " + ", ".join(f'<a href="{e(url)}">#{n}</a>' for n, _, url in it["linked"]))
        meta_html = " · ".join(m if m.startswith("fixes ") else e(m) for m in meta)
        rows.append(f"""<li class="row">
  <a class="row__ref" href="{e(it['url'])}">{e(ref(it, multi))}</a>
  <span class="row__main"><a class="row__title" href="{e(it['url'])}">{e(it['title'])}</a>
    <span class="row__meta">{meta_html}</span></span>
  {person(it)}
  <span class="row__when">{e(when)}</span>
</li>""")
    return f'<ul class="rows">{"".join(rows)}</ul>'


CSS = """
@font-face{font-display:swap;font-family:Geist;font-style:normal;font-weight:100 900;src:url('/fonts/Geist.woff2') format('woff2')}
@font-face{font-display:swap;font-family:'Geist Mono';font-style:normal;font-weight:100 900;src:url('/fonts/GeistMono.woff2') format('woff2')}
:root{--display:Geist,ui-sans-serif,system-ui,sans-serif;--faint:#a3a3a8;--gutter:4rem;--ink:#101012;--line:rgb(16 16 18/12%);
--mono:'Geist Mono',ui-monospace,'SF Mono',Menlo,monospace;--muted:#66666c;--paper:#f6f6f4;--plate:#fff;--rail:7.2rem;
--sans:Geist,ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;--ease:cubic-bezier(.22,1,.36,1)}
@media (max-width:767px){:root{--gutter:1.6rem;--rail:0rem}}
*{box-sizing:border-box}
html{background:var(--paper);color:var(--ink);font-size:max(9px,calc(min(100vw,1680px)/1440*10))}
@media (max-width:767px){html{font-size:calc(100vw/375*10)}}
body{margin:0;background:var(--paper);font:400 1.5rem/1.5 var(--sans)}
a{color:inherit;text-decoration:none}
p,h1,h2,.card__title{text-wrap:balance}
:focus-visible{outline:1px solid var(--ink);outline-offset:.3rem}
.glyph{fill:none;flex-shrink:0;height:1.8rem;width:1.8rem;stroke:currentcolor;stroke-linecap:round;stroke-linejoin:round;stroke-width:1.3}
.hidden{clip-path:inset(50%);height:1px;overflow:hidden;position:absolute;white-space:nowrap;width:1px}
.rail{align-items:center;bottom:0;display:flex;flex-direction:column;left:0;padding:1.8rem 0;position:fixed;top:0;width:7.2rem;z-index:20}
.rail__logo{display:block;height:3rem;width:3.6rem}.rail__logo svg{display:block;height:100%;width:100%}
.rail__items{display:flex;flex-direction:column;gap:.8rem;margin:auto 0}
.rail__item{align-items:center;border-radius:99rem;color:var(--muted);display:flex;height:4.4rem;justify-content:center;width:4.4rem;
transition:background .3s var(--ease),color .3s var(--ease)}
.rail__item:hover{background:rgb(16 16 18/6%);color:var(--ink)}
@media (max-width:767px){.rail{background:rgb(246 246 244/92%);backdrop-filter:blur(12px);border-top:1px solid var(--line);flex-direction:row;
height:6rem;justify-content:center;padding:0;top:auto;width:100%}.rail__logo{display:none}.rail__items{flex-direction:row;gap:2.4rem;margin:0}}
.page{padding-left:var(--rail)}
@media (max-width:767px){.page{padding-bottom:6rem}}
.top{align-items:center;display:flex;gap:1.6rem;height:7.2rem;justify-content:space-between;padding:0 var(--gutter)}
.top__tabs{display:flex;font-size:1.5rem;font-weight:500;gap:.4rem;margin-left:-1.4rem}
.top__tabs a{border-radius:99rem;color:var(--muted);padding:.8rem 1.4rem;transition:background .3s var(--ease),color .3s var(--ease)}
.top__tabs a:hover{background:rgb(16 16 18/6%);color:var(--ink)}
.top__tabs span{color:var(--faint);font-weight:400;margin-left:.4rem}
.top__actions{align-items:center;color:var(--muted);display:flex;font-size:1.4rem;gap:2rem;white-space:nowrap}
@media (max-width:767px){.top__tabs{display:none}}
.pill{align-items:center;border:1px solid var(--line);border-radius:99rem;color:var(--ink);display:inline-flex;font-size:1.4rem;font-weight:500;
gap:.6rem;padding:.8rem 1.6rem;position:relative;transition:border-color .3s var(--ease),background .3s var(--ease);white-space:nowrap;z-index:2}
.pill:hover{border-color:var(--ink)}.pill .glyph{height:1.4rem;width:1.4rem}
.pill--ink{background:var(--ink);border-color:var(--ink);color:var(--paper)}.pill--ink:hover{background:#2a2a2e}
main{padding:0 var(--gutter) 12rem}
.statement{align-items:center;display:flex;flex-direction:column;gap:2rem;padding:10.4rem 0 9.6rem;text-align:center}
.statement__title{font:500 7.2rem/1 var(--display);letter-spacing:-.025em;margin:0;max-width:96rem}
.statement__title span{color:var(--faint)}
.statement__text{color:var(--muted);font-size:1.8rem;line-height:1.4;margin:0;max-width:56rem}
.statement__text a{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:.3rem}
@media (max-width:767px){.statement{padding:5.6rem 0 4.8rem}.statement__title{font-size:4rem}}
section{margin-bottom:9.6rem;scroll-margin-top:2.4rem}
.bar{align-items:baseline;border-bottom:1px solid var(--line);display:flex;gap:1.6rem;justify-content:space-between;margin-bottom:2.4rem;padding-bottom:1.6rem}
.bar__title{font-size:1.5rem;font-weight:500;margin:0}.bar__title span{color:var(--faint);font-weight:400;margin-left:.6rem}
.bar__text{color:var(--muted);font-size:1.4rem;margin:0;text-align:right}
@media (max-width:767px){.bar{flex-direction:column;gap:.4rem}.bar__text{text-align:left}}
.cards{display:grid;gap:.8rem;grid-template-columns:repeat(2,minmax(0,1fr));margin:0 calc(var(--gutter)*-1 + .8rem)}
@media (max-width:1100px){.cards{grid-template-columns:minmax(0,1fr)}}
@media (max-width:767px){.cards{margin:0}}
.card{aspect-ratio:4/3;background-color:#ececea;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 20 20'%3E%3Cpath d='M0 .5h20M.5 0v20' fill='none' stroke='%23101012' stroke-opacity='.1' stroke-dasharray='1 3'/%3E%3C/svg%3E");
background-position:-.05rem -.05rem;background-size:2rem 2rem;border-radius:.4rem;display:flex;flex-direction:column;gap:1.6rem;
padding:1.8rem 2rem 2rem;position:relative;transition:background .4s var(--ease)}
.card:hover{background-color:#e6e6e3}
@media (max-width:767px){.card{aspect-ratio:auto}.card__figure{flex:none}}
.card__cover{inset:0;position:absolute;z-index:1}
.card__top{align-items:baseline;display:flex;gap:1.2rem;font-size:1.4rem}
.card__verdict{font-weight:500}.card__label{color:var(--muted);position:relative;z-index:2}.card__label:hover{color:var(--ink)}
.card__figure{align-items:center;display:flex;flex:1;justify-content:center;min-height:0;position:relative;z-index:2}
.card__figure img{border-radius:.2rem;display:block;max-height:100%;max-width:100%;object-fit:contain}
.card__figure--none{color:var(--faint);font-size:1.4rem;z-index:0}
.card__footer{display:flex;flex-direction:column;font-size:2.2rem;gap:.4rem;letter-spacing:-.02em;line-height:1.1}
.card__title{-webkit-box-orient:vertical;-webkit-line-clamp:2;display:-webkit-box;font-weight:500;overflow:hidden;position:relative;z-index:2}
.card__footer .person{color:var(--muted);gap:.8rem}.card__footer .avatar{height:1em;width:1em}
.card__caption{color:var(--muted);font-size:1.3rem;letter-spacing:0;line-height:1.4;margin-top:.4rem}
.card__caption a,.row__meta a{position:relative;text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:.3rem;z-index:2}
.card__actions{display:flex;gap:.8rem;letter-spacing:0;margin-top:1.2rem}
.person{align-items:center;display:inline-flex;gap:.8rem;min-width:0;position:relative;z-index:2}.person:hover{color:var(--ink)}
.avatar{background:radial-gradient(circle at var(--x) var(--y),rgb(255 255 255/55%),transparent 55%),
conic-gradient(from var(--angle),#ffd600,#b0f032,#00e8aa,#00f0ec,#00c8ff,#1496ff,#ffd600);border-radius:50%;display:inline-block;
flex-shrink:0;height:2rem;width:2rem}
.rows{list-style:none;margin:0;padding:0}
.row{align-items:baseline;border-bottom:1px solid var(--line);display:grid;gap:2rem;grid-template-columns:7.2rem minmax(0,1fr) 20rem 8rem;padding:1.6rem 0}
.row__ref,.row__when{color:var(--muted);font:400 1.3rem var(--mono)}.row__when{text-align:right}
.row__main{display:flex;flex-direction:column;gap:.2rem;min-width:0}
.row__title{font-weight:500}.row__title:hover,.row__ref:hover{color:var(--muted)}
.row__meta{color:var(--muted);font-size:1.3rem}
.row .person{color:var(--muted);font-size:1.4rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:767px){.row{gap:.4rem 1.2rem;grid-template-columns:5.6rem minmax(0,1fr)}.row .person{display:none}
.row__when{grid-column:2;text-align:left}}
.empty{color:var(--faint);margin:0}
.err{border:1px solid var(--line);border-radius:.4rem;color:var(--ink);margin:0 0 2.4rem;padding:1.2rem 1.6rem}
footer{color:var(--muted);font-size:1.3rem;padding:0 var(--gutter) 4rem}
footer a{text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:.3rem}
@media (max-width:767px){footer{padding-bottom:8rem}}
"""


def render_page(data: dict) -> str:
    multi = len(data["repos"]) > 1
    repos = ", ".join(f'<a href="https://github.com/{e(r)}">{e(r)}</a>' for r in data["repos"])
    errors = "".join(f'<p class="err">Could not read {e(msg)}</p>' for msg in data["errors"])
    fetched = data["fetched"].strftime("%H:%M UTC")
    n = len(data["needs_you"])
    if n:
        title = f'{n} pull request{"s" if n != 1 else ""} {"needs" if n == 1 else "need"} you. <span>The rest is in hand.</span>'
    else:
        title = 'Nothing needs you. <span>The rest is in hand.</span>'
    sections = [
        ("needs-you", "needs", "Needs You", n, "Open PRs with a review verdict, waiting on your merge or decision.",
         render_needs_you(data["needs_you"], multi)),
        ("in-progress", "progress", "Still Being Worked On", len(data["in_progress"]),
         "Open issues and PRs with no verdict yet. Issues with an open fix PR appear on that PR.",
         render_rows(data["in_progress"], multi)),
        ("landed", "landed", "Landed Recently", len(data["landed"]),
         f"Merged or closed in the last {data['landed_days']} days.",
         render_rows(data["landed"], multi, landed=True)),
    ]
    rail = "".join(
        f'<a aria-label="{label}" class="rail__item" href="#{sid}" title="{label}">{glyph(icon)}</a>'
        for sid, icon, label, *_ in sections
    )
    tabs = "".join(f'<a href="#{sid}">{label}<span>{count}</span></a>' for sid, _, label, count, *_ in sections)
    body = "".join(
        f'<section id="{sid}"><div class="bar"><h2 class="bar__title">{label}<span>{count}</span></h2>'
        f'<p class="bar__text">{text}</p></div>{content}</section>'
        for sid, _, label, count, text, content in sections
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#f6f6f4">
<link rel="preload" href="/fonts/Geist.woff2" as="font" type="font/woff2" crossorigin>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>Review dashboard ({n} need you)</title><style>{CSS}</style></head><body>
<nav aria-label="Sections" class="rail"><a class="rail__logo" href="/">{LOGO}<span class="hidden">Review dashboard</span></a>
<div class="rail__items">{rail}</div></nav>
<div class="page"><header class="top"><nav aria-label="Sections" class="top__tabs">{tabs}</nav>
<div class="top__actions">Updated {fetched}<a class="pill" href="/?refresh=1">{glyph("refresh")}Refresh</a></div></header>
<main>{errors}<header class="statement"><h1 class="statement__title">{title}</h1>
<p class="statement__text">Maintainer review dashboard for {repos}.</p></header>{body}</main>
<footer>Reads GitHub directly. <a href="{e(SESSION_URL)}">Request changes</a></footer></div></body></html>"""


STATIC = {
    "/fonts/Geist.woff2": ("static/fonts/Geist.woff2", "font/woff2"),
    "/fonts/GeistMono.woff2": ("static/fonts/GeistMono.woff2", "font/woff2"),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if u.path == "/api/items":
            d = dashboard_data("refresh" in parse_qs(u.query))
            return self._send(200, json.dumps(d, default=str), "application/json")
        if u.path == "/favicon.svg":
            return self._send(200, LOGO.replace('aria-hidden="true" ', 'xmlns="http://www.w3.org/2000/svg" '), "image/svg+xml")
        if u.path in STATIC:
            path, ctype = STATIC[u.path]
            return self._send(200, (ROOT / path).read_bytes(), ctype, "public, max-age=86400")
        if u.path != "/":
            return self._send(404, "not found", "text/plain")
        try:
            d = dashboard_data("refresh" in parse_qs(u.query))
            self._send(200, render_page(d), "text/html; charset=utf-8")
        except Exception as ex:
            self._send(500, f"<pre>{e(str(ex))}</pre>", "text/html; charset=utf-8")

    def _send(self, code: int, body: str | bytes, ctype: str, cache: str = "no-store"):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8601"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
