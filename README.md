# Maintainer review dashboard

Itai's review dashboard for the OSS maintenance Devin does. It reads GitHub directly and groups items, newest first:

- **Needs you**: open, non-draft PRs carrying a maintainer verdict (a formal `APPROVED` / `CHANGES_REQUESTED` review, or a review / review comment / PR comment containing `Verdict: <approve|request changes|close|...>`). Each card shows title, author (the original author for mirrored PRs), linked issue, verdict, the before/after GIF from the verdict, PR body or comments, and GitHub links.
- **Still being worked on**: open issues and PRs with no verdict yet. An issue with an open PR that links it appears on that PR instead.
- **Landed recently**: issues and PRs merged or closed in the last `landed_days` days.

## Configuration

`config.json`:

```json
{ "repos": ["itaismith/manim-mock"], "landed_days": 14, "cache_seconds": 60 }
```

Add repositories to `repos` (e.g. `"3b1b/manim"`), or override with `DASHBOARD_REPOS=owner/a,owner/b`.

GitHub auth: `GITHUB_TOKEN` if set, otherwise `gh auth token` (the box's GitHub connection), refreshed every 10 minutes.

## Run

```sh
pip install -r requirements.txt
PORT=8601 python3 app.py
```

Endpoints: `/` (dashboard, `?refresh=1` skips the 60 s cache), `/api/items` (JSON), `/healthz`.
