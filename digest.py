"""Paper subscription digest.

Pulls ToC RSS feeds from IJRR, Science Robotics, T-RO, plus the arXiv
``cs.RO`` listing (optionally filtered by keywords/authors in
``config.json``). An optional post-fetch topic ``filter`` (grouped
keyword matching) is applied to every source. New entries are deduped
against ``state.json``, written
as a Markdown digest under ``digests/``, and emailed via SMTP when
``SMTP_HOST`` and ``SMTP_TO`` are set in the environment.

Designed to run unattended in GitHub Actions; safe to re-run locally.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import sys
import urllib.parse
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Iterable

import feedparser

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.json"
DIGEST_DIR = ROOT / "digests"
MAX_PER_SOURCE = 500

USER_AGENT = (
    "paper-subscription-bot/1.0 "
    "(+https://github.com/BraveBoBo/paper-subscription)"
)

JOURNAL_FEEDS: dict[str, str] = {
    "IJRR": (
        "https://journals.sagepub.com/action/showFeed"
        "?ui=0&mi=ehikzz&ai=2b4&jc=ijra&type=etoc&feed=rss"
    ),
    "Science Robotics": (
        "https://www.science.org/action/showFeed"
        "?type=etoc&feed=rss&jc=scirobotics"
    ),
    "T-RO": "https://ieeexplore.ieee.org/rss/TOC8860.XML",
}

DEFAULT_CONFIG: dict = {
    "arxiv": {
        "enabled": True,
        "categories": ["cs.RO"],
        "keywords": [],
        "authors": [],
        "max_results": 50,
    },
    # Post-fetch topic filter applied to every source (journals included).
    # Each group is OR-matched internally; a paper must match ALL groups.
    "filter": {
        "enabled": False,
        "groups": [],
    },
}


def load_state() -> dict[str, list[str]]:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, list[str]]) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("[warn] config.json invalid, using defaults", file=sys.stderr)
        return DEFAULT_CONFIG
    merged = {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULT_CONFIG.items()}
    for k, v in (data or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def build_arxiv_url(arxiv_cfg: dict) -> str:
    categories = arxiv_cfg.get("categories") or ["cs.RO"]
    keywords = arxiv_cfg.get("keywords") or []
    authors = arxiv_cfg.get("authors") or []
    max_results = int(arxiv_cfg.get("max_results") or 50)

    cat_clause = " OR ".join(f"cat:{c}" for c in categories)
    groups: list[str] = []
    if keywords:
        groups.append(" OR ".join(f'all:"{k}"' for k in keywords))
    if authors:
        groups.append(" OR ".join(f'au:"{a}"' for a in authors))

    if groups:
        combined = " OR ".join(f"({g})" for g in groups)
        query = f"({cat_clause}) AND ({combined})"
    else:
        query = f"({cat_clause})"

    return (
        "http://export.arxiv.org/api/query?"
        f"search_query={urllib.parse.quote(query)}"
        "&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results}"
    )


def entry_key(entry) -> str:
    """Stable per-paper key, version-stripped for arXiv IDs."""
    entry_id = (entry.get("id") or "").strip()
    if "arxiv.org/abs/" in entry_id:
        arxiv_part = entry_id.split("arxiv.org/abs/")[-1]
        base, _, ver = arxiv_part.rpartition("v")
        if base and ver.isdigit():
            arxiv_part = base
        return f"arxiv:{arxiv_part}"
    for attr in ("id", "link", "title"):
        value = entry.get(attr)
        if value:
            return str(value).strip()
    return ""


def _authors(entry) -> str:
    raw = entry.get("authors") or []
    names: list[str] = []
    for author in raw:
        name = author.get("name") if isinstance(author, dict) else None
        if name:
            names.append(name.strip())
    if not names and entry.get("author"):
        names.append(str(entry["author"]).strip())
    return ", ".join(names)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_summary(text: str) -> str:
    text = _TAG_RE.sub("", text or "")
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > 800:
        text = text[:800].rstrip() + "…"
    return text


def matches_filter(item: dict, filter_cfg: dict) -> bool:
    """True if ``item`` matches the topic filter (case-insensitive substring).

    Each group is OR-matched against the title/summary/authors text; the item
    must match *every* group. Empty/disabled filters accept everything.
    """
    if not filter_cfg or not filter_cfg.get("enabled"):
        return True
    groups = filter_cfg.get("groups") or []
    if not groups:
        return True
    text = " ".join(
        str(item.get(field, "")) for field in ("title", "summary", "authors")
    ).lower()
    for group in groups:
        terms = [str(t).lower() for t in (group or []) if t]
        if terms and not any(term in text for term in terms):
            return False
    return True


def fetch_feed(
    name: str, url: str, seen: Iterable[str], filter_cfg: dict | None = None
) -> tuple[list[dict], list[str]]:
    print(f"[fetch] {name}: {url}")
    parsed = feedparser.parse(url, agent=USER_AGENT)
    if parsed.bozo:
        print(f"[warn] {name}: bozo={parsed.bozo_exception!r}")

    seen_set = set(seen)
    new_items: list[dict] = []
    new_keys: list[str] = []
    skipped = 0
    for entry in parsed.entries:
        key = entry_key(entry)
        if not key or key in seen_set:
            continue
        item = {
            "source": name,
            "title": (entry.get("title") or "").strip(),
            "link": (entry.get("link") or "").strip(),
            "summary": _clean_summary(entry.get("summary") or ""),
            "published": (
                entry.get("published") or entry.get("updated") or ""
            ).strip(),
            "authors": _authors(entry),
        }
        # Non-matching entries are skipped without being marked seen, so they
        # resurface automatically if the filter is later broadened.
        if not matches_filter(item, filter_cfg or {}):
            skipped += 1
            continue
        seen_set.add(key)
        new_keys.append(key)
        new_items.append(item)
    print(
        f"[fetch] {name}: {len(new_items)} new / {len(parsed.entries)} total"
        f" ({skipped} filtered)"
    )
    return new_items, new_keys


def render_markdown(items: list[dict], when: datetime, order: list[str]) -> str:
    lines = [
        f"# Paper digest — {when.strftime('%Y-%m-%d')}",
        "",
        f"Generated at {when.isoformat()} ({len(items)} new papers).",
        "",
    ]
    by_source: dict[str, list[dict]] = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)
    for source in order:
        bucket = by_source.get(source, [])
        lines.append(f"## {source} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("_No new entries._")
            lines.append("")
            continue
        for item in bucket:
            title = item["title"] or "(untitled)"
            if item["link"]:
                lines.append(f"### [{title}]({item['link']})")
            else:
                lines.append(f"### {title}")
            if item["authors"]:
                lines.append(f"*{item['authors']}*")
            if item["published"]:
                lines.append(f"_{item['published']}_")
            if item["summary"]:
                lines.append("")
                lines.append(item["summary"])
            lines.append("")
    return "\n".join(lines)


def render_html(items: list[dict], when: datetime, order: list[str]) -> str:
    parts = [
        '<!doctype html><html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
        'max-width:760px;line-height:1.5;color:#222;">',
        f"<h1>Paper digest — {escape(when.strftime('%Y-%m-%d'))}</h1>",
        f"<p>{len(items)} new papers, generated at "
        f"{escape(when.isoformat())}.</p>",
    ]
    by_source: dict[str, list[dict]] = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)
    for source in order:
        bucket = by_source.get(source, [])
        parts.append(
            f"<h2>{escape(source)} <small>({len(bucket)})</small></h2>"
        )
        if not bucket:
            parts.append("<p><em>No new entries.</em></p>")
            continue
        for item in bucket:
            title = escape(item["title"] or "(untitled)")
            link = item["link"]
            if link:
                parts.append(
                    f'<h3><a href="{escape(link)}">{title}</a></h3>'
                )
            else:
                parts.append(f"<h3>{title}</h3>")
            if item["authors"]:
                parts.append(
                    f'<p style="margin:.2em 0"><em>'
                    f'{escape(item["authors"])}</em></p>'
                )
            if item["published"]:
                parts.append(
                    f'<p style="margin:.2em 0;color:#666"><small>'
                    f'{escape(item["published"])}</small></p>'
                )
            if item["summary"]:
                parts.append(f'<p>{escape(item["summary"])}</p>')
    parts.append("</body></html>")
    return "\n".join(parts)


def send_email(subject: str, text_body: str, html_body: str) -> bool:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        print("[email] SMTP_HOST not set, skipping.")
        return False
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = (os.environ.get("SMTP_FROM") or user).strip()
    to_raw = os.environ.get("SMTP_TO", "").strip()
    if not sender or not to_raw:
        print("[email] SMTP_FROM/SMTP_TO missing, skipping.")
        return False
    recipients = [r.strip() for r in to_raw.split(",") if r.strip()]
    use_ssl = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    print(
        f"[email] sending to {recipients} via {host}:{port} "
        f"(ssl={use_ssl}, tls={use_tls})"
    )
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            if use_tls:
                s.starttls()
                s.ehlo()
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    print("[email] sent")
    return True


def write_github_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"{key}={value}\n")


def main() -> int:
    config = load_config()
    state = load_state()

    sources: list[tuple[str, str]] = list(JOURNAL_FEEDS.items())
    arxiv_cfg = config.get("arxiv", {}) or {}
    if arxiv_cfg.get("enabled", True):
        cats = arxiv_cfg.get("categories") or ["cs.RO"]
        arxiv_name = "arXiv " + ",".join(cats)
        sources.append((arxiv_name, build_arxiv_url(arxiv_cfg)))

    source_order = [name for name, _ in sources]
    all_new: list[dict] = []
    filter_cfg = config.get("filter", {}) or {}

    for name, url in sources:
        seen = state.get(name, [])
        try:
            new_items, new_keys = fetch_feed(name, url, seen, filter_cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] {name}: {exc!r}", file=sys.stderr)
            continue
        if new_keys:
            state[name] = (new_keys + list(seen))[:MAX_PER_SOURCE]
        all_new.extend(new_items)

    save_state(state)

    now = datetime.now(timezone.utc)
    new_count = len(all_new)
    write_github_output("new_count", str(new_count))

    if new_count == 0:
        print("[done] no new papers")
        write_github_output("digest_path", "")
        return 0

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = DIGEST_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    md_body = render_markdown(all_new, now, source_order)
    digest_path.write_text(md_body, encoding="utf-8")
    write_github_output("digest_path", str(digest_path.relative_to(ROOT)))
    print(f"[done] wrote {digest_path} ({new_count} new papers)")

    try:
        send_email(
            subject=f"[paper digest] {now.strftime('%Y-%m-%d')} — {new_count} new",
            text_body=md_body,
            html_body=render_html(all_new, now, source_order),
        )
    except Exception as exc:  # noqa: BLE001
        # Email failure shouldn't fail the workflow — digest is already on disk.
        print(f"[email] send failed: {exc!r}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
