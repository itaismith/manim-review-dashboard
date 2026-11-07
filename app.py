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
        if url.endswith(".svg") or "devin-review" in url:
            continue
        path = urlparse(url).path.lower()
        name = _jwt_filename(url).lower()
        label = alt.lower()
        if path.endswith(".gif") or name.endswith(".gif") or "before" in label or "after" in label or "gif" in label:
            return url, alt
    return None


def linked_issue(body: str, issue_numbers: set[int]) -> int | None:
    """Closing keyword ("Fixes #2") first, else the first mention of a known issue of this repo."""
    m = LINKED_RE.search(body)
    if m:
        return int(m.group(1))
    for n in MENTION_RE.findall(body):
        if int(n) in issue_numbers:
            return int(n)
    return None


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


def author_of(item: dict) -> tuple[str, str]:
    """(display author, profile url); mirrored items credit the original author."""
    m = ORIGIN_RE.search(item.get("body") or "")
    if m:
        return m.group(1), f"https://github.com/{m.group(1)}"
    login = item["user"]["login"]
    return login, item["user"]["html_url"]


# ---------- data collection ----------

def pr_details(repo: str, pr: dict) -> dict:
    n = pr["number"]
    reviews = gh_get(f"/repos/{repo}/pulls/{n}/reviews")
    comments = gh_get(f"/repos/{repo}/issues/{n}/comments")
    review_comments = gh_get(f"/repos/{repo}/pulls/{n}/comments")

    events = []  # (time, verdict, body, url, who)
    for r in reviews:
        v = {"APPROVED": "Approve", "CHANGES_REQUESTED": "Changes requested"}.get(r["state"]) or find_verdict(r.get("body"))
        if v:
            events.append((r.get("submitted_at"), v, r.get("body") or "", r.get("html_url"), r["user"]["login"]))
    for c in comments + review_comments:
        v = find_verdict(c.get("body"))
        if v:
            events.append((c.get("created_at") or c.get("updated_at"), v, c.get("body") or "", c.get("html_url"), c["user"]["login"]))
    events.sort(key=lambda e: e[0] or "")
    verdict = events[-1] if events else None

    gif = None
    bodies = ([verdict[2]] if verdict else []) + [pr.get("body") or ""] + [r.get("body") or "" for r in reviews] + [c.get("body") or "" for c in comments]
    for b in bodies:
        gif = find_gif(b)
        if gif:
            break
    return {"verdict": verdict, "gif": gif}


def collect_repo(repo: str, landed_days: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=landed_days)
    open_items = gh_get(f"/repos/{repo}/issues", {"state": "open"})
    closed_items = gh_get(f"/repos/{repo}/issues", {"state": "closed", "since": since.isoformat()})
    titles = {i["number"]: i["title"] for i in open_items + closed_items}
    issue_numbers = {i["number"] for i in open_items + closed_items if "pull_request" not in i}

    open_prs = [i for i in open_items if "pull_request" in i]
    with ThreadPoolExecutor(max_workers=6) as ex:
        details = dict(zip([p["number"] for p in open_prs], ex.map(lambda p: pr_details(repo, p), open_prs)))

    needs_you, in_progress, landed = [], [], []
    linked_by_open_pr = set()

    for p in open_prs:
        d = details[p["number"]]
        linked = linked_issue(p.get("body") or "", issue_numbers)
        if linked:
            linked_by_open_pr.add(linked)
        who, who_url = author_of(p)
        item = {
            "repo": repo, "kind": "PR", "number": p["number"], "title": p["title"], "url": p["html_url"],
            "author": who, "author_url": who_url, "updated": parse_time(p["updated_at"]),
            "draft": p.get("draft", False),
            "linked": (linked, titles.get(linked), f"https://github.com/{repo}/issues/{linked}") if linked else None,
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
            r = collect_repo(repo, cfg["landed_days"])
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


def verdict_class(v: str) -> str:
    return {"Approve": "ok", "Changes requested": "warn", "Close": "bad"}.get(v, "neutral")


def render_needs_you(items: list, multi: bool) -> str:
    if not items:
        return '<p class="empty">Nothing is waiting on you.</p>'
    out = []
    for it in items:
        linked = ""
        if it.get("linked"):
            n, title, url = it["linked"]
            linked = f'<div class="meta">Fixes <a href="{e(url)}">#{n}</a>{" " + e(title) if title else ""}</div>'
        gif = ""
        if it.get("gif"):
            url, alt = it["gif"]
            gif = f'<a class="gif" href="{e(url)}" target="_blank"><img src="{e(url)}" alt="{e(alt or "before/after")}" loading="lazy"></a>'
        else:
            gif = '<div class="gif none">No before/after GIF</div>'
        out.append(f"""
<article class="card">
  <div class="info">
  <div class="card-head">
    <span class="pill {verdict_class(it['verdict'])}">{e(it['verdict'])}</span>
    <a class="num mono" href="{e(it['url'])}">{e(ref(it, multi))}</a>
    <h3><a href="{e(it['url'])}">{e(it['title'])}</a></h3>
  </div>
  <div class="meta">by <a href="{e(it['author_url'])}">@{e(it['author'])}</a> · verdict
    <a href="{e(it.get('verdict_url') or it['url'])}">{e(ago(it.get('verdict_time')))}</a></div>
  {linked}
  <div class="actions"><a class="btn" href="{e(it['url'])}">Open on GitHub</a>
    <a class="link" href="{e(it['url'])}/files">Files changed</a></div>
  </div>
  {gif}
</article>""")
    return "\n".join(out)


def render_rows(items: list, multi: bool, landed: bool = False) -> str:
    if not items:
        return '<p class="empty">Nothing here.</p>'
    rows = []
    for it in items:
        if landed:
            status = f'<span class="pill {"ok" if it["outcome"] == "Merged" else "neutral"}">{e(it["outcome"])}</span>'
            when = ago(it["closed"])
        else:
            status = '<span class="pill neutral">Draft</span>' if it.get("draft") else ""
            when = ago(it["updated"])
        extra = ""
        if it.get("linked"):
            n, _, url = it["linked"]
            extra = f' <span class="muted">· fixes <a href="{e(url)}">#{n}</a></span>'
        rows.append(f"""<tr>
  <td class="kind">{e(it['kind'])}</td>
  <td class="mono"><a href="{e(it['url'])}">{e(ref(it, multi))}</a></td>
  <td class="title"><a href="{e(it['url'])}">{e(it['title'])}</a>{extra} {status}</td>
  <td class="who"><a href="{e(it['author_url'])}">@{e(it['author'])}</a></td>
  <td class="when mono">{e(when)}</td>
</tr>""")
    return f'<table><tbody>{"".join(rows)}</tbody></table>'


CSS = """
:root{--bg:#FCFCFC;--surface:#FFFFFF;--text:#191919;--text-muted:rgba(25,25,25,.56);--border:rgba(0,0,0,.10);
--accent:#317CFF;--accent-fg:#FFFFFF;--primary-action:var(--accent);--primary-action-fg:var(--accent-fg);--radius:6px;
--font:Inter,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;
--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--font)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{border-bottom:1px solid var(--border);background:var(--surface)}
header .bar{max-width:1100px;margin:0 auto;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;padding:12px 20px}
header h1{font-size:16px;margin:0}header .repos{color:var(--text-muted);font-size:13px}
main{max-width:1100px;margin:0 auto;padding:16px 20px 40px}
section{margin-top:20px}section h2{font-size:14px;margin:0 0 8px;display:flex;gap:8px;align-items:baseline}
section h2 .count{font-family:var(--mono);color:var(--text-muted);font-weight:400}
section .sub{color:var(--text-muted);font-size:13px;margin:-4px 0 10px}
.needs{border:1px solid var(--accent);border-radius:var(--radius);padding:14px;background:var(--surface)}
.cards{display:grid;gap:12px}
.card{border:1px solid var(--border);border-radius:var(--radius);padding:12px;background:var(--surface);display:grid;grid-template-columns:minmax(0,2fr) minmax(0,3fr);gap:12px}
.card .info{display:flex;flex-direction:column;gap:6px}
@media (max-width:760px){.card{grid-template-columns:1fr}}
.card-head{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:baseline}.card h3{font-size:14px;margin:0;flex-basis:100%}
.card h3 a{color:var(--text)}.meta,.muted{color:var(--text-muted);font-size:13px}
.gif img{width:100%;border:1px solid var(--border);border-radius:var(--radius);display:block;background:#000}
.gif.none{border:1px dashed var(--border);border-radius:var(--radius);padding:16px;text-align:center;color:var(--text-muted);font-size:13px}
.actions{display:flex;gap:12px;align-items:center;margin-top:4px}
.btn{background:var(--primary-action);color:var(--primary-action-fg);padding:5px 10px;border-radius:var(--radius);font-size:13px}
.btn:hover{text-decoration:none;opacity:.9}.link{font-size:13px}
.pill{display:inline-block;font-size:12px;padding:1px 7px;border-radius:999px;border:1px solid var(--border);white-space:nowrap}
.pill.ok{color:var(--ok);border-color:currentColor}.pill.warn{color:var(--warn);border-color:currentColor}
.pill.bad{color:var(--bad);border-color:currentColor}.pill.neutral{color:var(--text-muted)}
.mono{font-family:var(--mono);font-size:12.5px}.num{color:var(--text-muted)}
table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius)}
td{padding:7px 10px;border-top:1px solid var(--border);vertical-align:top}tr:first-child td{border-top:0}
td.kind{color:var(--text-muted);width:48px;font-size:12px;padding-top:9px}td.title>a{color:var(--text)}
td.who{white-space:nowrap;font-size:13px}td.when{white-space:nowrap;color:var(--text-muted);text-align:right}
.empty{color:var(--text-muted);margin:0}.err{color:var(--bad);border:1px solid currentColor;border-radius:var(--radius);padding:8px 12px}
footer{max-width:1100px;margin:0 auto;padding:0 20px 24px;color:var(--text-muted);font-size:12px}
@media (max-width:600px){td.who,td.kind{display:none}header .bar{padding:10px 14px}main{padding:12px 14px 32px}}
"""


def render_page(data: dict) -> str:
    multi = len(data["repos"]) > 1
    repos = ", ".join(f'<a href="https://github.com/{e(r)}">{e(r)}</a>' for r in data["repos"])
    errors = "".join(f'<p class="err">Could not read {e(msg)}</p>' for msg in data["errors"])
    fetched = data["fetched"].strftime("%H:%M UTC")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Review dashboard ({len(data['needs_you'])} need you)</title><style>{CSS}</style></head><body>
<header><div class="bar"><div><h1>Maintainer review dashboard</h1><div class="repos">{repos}</div></div>
<div class="muted">Updated {fetched} · <a href="/?refresh=1">Refresh</a></div></div></header>
<main>{errors}
<section class="needs" id="needs-you"><h2>Needs you <span class="count">{len(data['needs_you'])}</span></h2>
<p class="sub">Open PRs with a review verdict, waiting on your merge or decision.</p>
<div class="cards">{render_needs_you(data['needs_you'], multi)}</div></section>
<section id="in-progress"><h2>Still being worked on <span class="count">{len(data['in_progress'])}</span></h2>
<p class="sub">Open issues and PRs with no verdict yet. Issues with an open fix PR appear on that PR.</p>
{render_rows(data['in_progress'], multi)}</section>
<section id="landed"><h2>Landed recently <span class="count">{len(data['landed'])}</span></h2>
<p class="sub">Merged or closed in the last {data['landed_days']} days.</p>
{render_rows(data['landed'], multi, landed=True)}</section>
</main><footer>Reads GitHub directly. <a href="{e(SESSION_URL)}">Request changes</a></footer></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if u.path == "/api/items":
            d = dashboard_data("refresh" in parse_qs(u.query))
            return self._send(200, json.dumps(d, default=str), "application/json")
        if u.path != "/":
            return self._send(404, "not found", "text/plain")
        try:
            d = dashboard_data("refresh" in parse_qs(u.query))
            self._send(200, render_page(d), "text/html; charset=utf-8")
        except Exception as ex:
            self._send(500, f"<pre>{e(str(ex))}</pre>", "text/html; charset=utf-8")

    def _send(self, code: int, body: str, ctype: str):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8601"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
