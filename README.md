# paper-subscription

A small, unattended digest bot that pulls new robotics papers from journal
RSS feeds and arXiv, filters them down to the topics you care about, writes a
Markdown digest, and emails it to you. Designed to run daily in GitHub
Actions; safe to re-run locally.

## Topics

This instance is focused on **VLA-memory** and **force-VLA** research, i.e.
Vision-Language-Action models that involve memory or force/tactile feedback.

## Sources

| Source | Type | Filtered |
|--------|------|----------|
| IJRR | journal ToC RSS | yes (post-fetch) |
| Science Robotics | journal ToC RSS | yes (post-fetch) |
| T-RO | journal ToC RSS | yes (post-fetch) |
| arXiv `cs.RO` | search API | yes (server-side keywords + post-fetch) |

## How it works

1. **Fetch** each source's feed (`feedparser`).
2. **Dedupe** entries against `state.json` (arXiv IDs are version-stripped, so
   `2401.12345v2` and `…v3` count as the same paper).
3. **Filter** every entry through the topic filter (see below).
4. **Render** the surviving papers to `digests/YYYY-MM-DD.md`.
5. **Email** the digest via SMTP (only if `SMTP_HOST`/`SMTP_TO` are set).
6. The GitHub Actions workflow commits the updated `state.json` and new digest
   back to the repo.

## Topic filter

Configured in `config.json`. The filter uses **grouped substring matching**
(case-insensitive, over title + summary + authors):

```json
"filter": {
  "enabled": true,
  "groups": [
    ["vla", "vision-language-action", "vision language action"],
    ["memory", "force", "tactile", "haptic"]
  ]
}
```

- **Within a group → OR**, **across groups → AND.**
- A paper is kept only if it matches **at least one term in every group** —
  here, a VLA term **and** a memory/force term. This is what keeps the journal
  feeds from flooding the digest with every paper that merely says "force".
- Non-matching entries are **not** recorded in `state.json`, so if you later
  broaden the groups, previously-skipped papers resurface automatically.
- Set `"enabled": false` (or empty `groups`) to pass everything through.

arXiv additionally pre-filters server-side via `arxiv.keywords` to limit how
many results are fetched before the local filter runs.

## Configuration (`config.json`)

```json
{
  "arxiv": {
    "enabled": true,
    "categories": ["cs.RO"],
    "keywords": ["VLA memory", "force VLA", "..."],
    "authors": [],
    "max_results": 50
  },
  "filter": {
    "enabled": true,
    "groups": [["vla", "..."], ["memory", "force", "..."]]
  }
}
```

## Email (environment variables)

Email is skipped unless `SMTP_HOST` and `SMTP_TO` are set. In GitHub Actions
these come from repository secrets.

| Variable | Default | Notes |
|----------|---------|-------|
| `SMTP_HOST` | — | required to send |
| `SMTP_TO` | — | required; comma-separated |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` / `SMTP_PASSWORD` | — | optional auth |
| `SMTP_FROM` | `SMTP_USER` | |
| `SMTP_USE_TLS` | `true` | STARTTLS |
| `SMTP_USE_SSL` | `false` | implicit TLS |

## Running locally

```bash
pip install -r requirements.txt
python digest.py
```

Output is written to `digests/`. Without SMTP env vars set, the email step is
skipped and the digest is left on disk.

## Schedule

`.github/workflows/paper-digest.yml` runs daily at **01:00 UTC** (09:00
Asia/Shanghai) and on manual `workflow_dispatch`. Scheduled runs only fire on
the repository's default branch.
