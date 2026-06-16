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
README_FILE = ROOT / "README.md"
# The latest digest is injected into README.md between these markers.
README_DIGEST_START = "<!-- DIGEST:START -->"
README_DIGEST_END = "<!-- DIGEST:END -->"
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


def _matches_groups(text: str, groups: list) -> bool:
    """True if ``text`` satisfies every group (OR within a group, AND across)."""
    for group in groups:
        terms = [str(t).lower() for t in (group or []) if t]
        if terms and not any(term in text for term in terms):
            return False
    return True


ACCEPT_ALL = "*"  # sentinel topic: filter disabled / a topic with no groups.


def classify_topics(item: dict, filter_cfg: dict) -> list[str]:
    """Topic names whose groups all match ``item`` (case-insensitive substring).

    The filter holds one or more *topics*. Within a topic each group is
    OR-matched against the title/summary/authors text and the item must satisfy
    *every* group (AND). Returns the ``name`` of **every** topic the item
    satisfies (OR across topics), so each paper can be rendered under its
    topic section. Empty/disabled filters — and topics with no groups — accept
    everything and return ``[ACCEPT_ALL]``.

    Two config shapes are accepted:

    * ``{"topics": [{"name": ..., "groups": [...]}, ...]}`` — multi-topic.
    * ``{"groups": [...]}`` — legacy single topic (still supported).
    """
    if not filter_cfg or not filter_cfg.get("enabled"):
        return [ACCEPT_ALL]

    topics = filter_cfg.get("topics")
    if not topics:
        groups = filter_cfg.get("groups") or []
        if not groups:
            return [ACCEPT_ALL]
        topics = [{"name": ACCEPT_ALL, "groups": groups}]

    text = " ".join(
        str(item.get(field, "")) for field in ("title", "summary", "authors")
    ).lower()

    matched: list[str] = []
    for topic in topics:
        if isinstance(topic, dict):
            groups = topic.get("groups") or []
            name = topic.get("name") or ACCEPT_ALL
        else:
            groups, name = topic or [], ACCEPT_ALL
        if not groups:
            # A topic with no groups matches everything.
            return [ACCEPT_ALL]
        if _matches_groups(text, groups):
            matched.append(name)
    return matched


def matches_filter(item: dict, filter_cfg: dict) -> bool:
    """True if ``item`` matches any topic (see :func:`classify_topics`)."""
    return bool(classify_topics(item, filter_cfg))


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
        topics = classify_topics(item, filter_cfg or {})
        if not topics:
            skipped += 1
            continue
        item["topics"] = topics
        seen_set.add(key)
        new_keys.append(key)
        new_items.append(item)
    print(
        f"[fetch] {name}: {len(new_items)} new / {len(parsed.entries)} total"
        f" ({skipped} filtered)"
    )
    return new_items, new_keys


def render_markdown(items: list[dict], when: datetime, order: list[dict]) -> str:
    lines = [
        f"# Paper digest — {when.strftime('%Y-%m-%d')}",
        "",
        f"Generated at {when.isoformat()} ({len(items)} new papers).",
        "",
    ]
    for header, bucket in _sections(items, order):
        lines.append(f"## {header} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("_No new entries._")
            lines.append("")
            continue
        for item in bucket:
            lines.append(_entry_line(item))
            if item.get("summary"):
                lines.append("")
                lines.append(f"  {item['summary']}")
            lines.append("")
    return "\n".join(lines)


def _short_authors(authors: str, max_n: int = 3) -> str:
    """First few author names, with 'et al.' when the list is longer."""
    names = [a.strip() for a in (authors or "").split(",") if a.strip()]
    if not names:
        return ""
    if len(names) <= max_n:
        return ", ".join(names)
    return ", ".join(names[:max_n]) + " et al."


# --- awesome-list entry rendering (model name + badges, grouped by topic) ----

_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/(\d{4}\.\d{4,5})")
_WEBSITE_RE = re.compile(
    r"https?://(?:github\.com|huggingface\.co|sites\.google\.com|"
    r"[\w.-]+\.github\.io)/\S+"
)


def _arxiv_id(link: str) -> str | None:
    """Version-stripped arXiv id in ``link``, or ``None``."""
    m = _ARXIV_ID_RE.search(link or "")
    return m.group(1) if m else None


def _model_name(title: str) -> str | None:
    """Leading ``Name:`` segment of a title, used as the model/method name.

    Matches the awesome-list convention (``RT-2``, ``SmolVLA``, ``Qwen-VLA``):
    only the short pre-colon part of ``Name: Full title`` qualifies.
    """
    head, sep, _ = (title or "").partition(":")
    head = head.strip()
    if not sep or not head or len(head.split()) > 5:
        return None
    return head


def _website(summary: str) -> str | None:
    """First project/code URL found in ``summary`` (best-effort)."""
    m = _WEBSITE_RE.search(summary or "")
    return m.group(0).rstrip(".,);") if m else None


def _badges(item: dict) -> str:
    """Markdown shields for an item: arXiv (or Paper) + optional Website."""
    parts: list[str] = []
    aid = _arxiv_id(item.get("link", ""))
    if aid:
        parts.append(
            f"[![arXiv](https://img.shields.io/badge/arXiv-{aid}-b31b1b.svg)]"
            f"(https://arxiv.org/abs/{aid})"
        )
    elif item.get("link"):
        parts.append(
            "[![Paper](https://img.shields.io/badge/Paper-Link-blue)]"
            f"({item['link']})"
        )
    site = _website(item.get("summary", ""))
    if site:
        parts.append(
            f"[![Website](https://img.shields.io/badge/Website-Link-blue)]({site})"
        )
    return " ".join(parts)


def _entry_line(item: dict) -> str:
    """One awesome-list bullet: ``- **Model**, Title. <badges> — *authors*``."""
    title = item.get("title") or "(untitled)"
    model = _model_name(title)
    head = f"**{model}**, {title}." if model else f"**{title}**."
    line = f"- {head}"
    badges = _badges(item)
    if badges:
        line += f" {badges}"
    authors = _short_authors(item.get("authors", ""))
    if authors:
        line += f" — *{authors}*"
    return line


def _by_topic(
    items: list[dict], order: list[dict]
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Bucket items by topic name (declared order); cross-topic papers repeat.

    Returns ``(buckets, uncategorized)``; the latter holds items whose topics
    are not in ``order`` (only possible when the filter is disabled).
    """
    buckets: dict[str, list[dict]] = {t["name"]: [] for t in order}
    uncategorized: list[dict] = []
    for item in items:
        names = [n for n in item.get("topics", []) if n in buckets]
        if names:
            for n in names:
                buckets[n].append(item)
        else:
            uncategorized.append(item)
    return buckets, uncategorized


def _sections(items: list[dict], order: list[dict]) -> list[tuple[str, list[dict]]]:
    """``[(header, bucket), ...]`` in display order, with a trailing catch-all."""
    buckets, uncategorized = _by_topic(items, order)
    out = [
        (f"{t.get('emoji', '')} {t.get('title') or t['name']}".strip(), buckets[t["name"]])
        for t in order
    ]
    if uncategorized:
        out.append(("Uncategorized", uncategorized))
    return out


def _topic_order(filter_cfg: dict) -> list[dict]:
    """Display order of topics ``[{name, title, emoji}, ...]`` from the config.

    Empty when the filter is disabled / has no named topics; rendering then
    falls back to the single ``Uncategorized`` catch-all.
    """
    order: list[dict] = []
    for topic in (filter_cfg or {}).get("topics") or []:
        if isinstance(topic, dict) and topic.get("name"):
            order.append(
                {
                    "name": topic["name"],
                    "title": topic.get("title") or topic["name"],
                    "emoji": topic.get("emoji") or "",
                }
            )
    return order


def render_compact(items: list[dict], when: datetime, order: list[dict]) -> str:
    """A condensed digest for the README: one bullet per paper, no abstracts.

    All topic sections are shown in order (empty ones included), so the README
    always lists every topic.
    """
    lines: list[str] = []
    for header, bucket in _sections(items, order):
        lines.append(f"## {header} ({len(bucket)})")
        if not bucket:
            lines.append("_No new entries._")
        else:
            for item in bucket:
                lines.append(_entry_line(item))
        lines.append("")
    if not lines:
        lines.append("_No new entries._")
    return "\n".join(lines).rstrip() + "\n"


_HEADING_RE = re.compile(r"^(#{1,6})(\s)")


def _demote_headings(md: str, by: int = 2) -> str:
    """Shift every ATX heading down ``by`` levels (capped at H6).

    Keeps the injected digest nested under the README's own "Latest digest"
    heading instead of competing with the page's top-level headings.
    """
    out: list[str] = []
    for line in md.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            level = min(len(m.group(1)) + by, 6)
            line = "#" * level + line[len(m.group(1)):]
        out.append(line)
    return "\n".join(out)


def update_readme(md_body: str) -> bool:
    """Replace the README's DIGEST section with the latest digest.

    Returns ``True`` when the README was rewritten, ``False`` when it is
    missing or has no marker pair (in which case the run is unaffected).
    """
    if not README_FILE.exists():
        return False
    text = README_FILE.read_text(encoding="utf-8")
    if README_DIGEST_START not in text or README_DIGEST_END not in text:
        return False
    pre, _, rest = text.partition(README_DIGEST_START)
    _, _, post = rest.partition(README_DIGEST_END)
    section = (
        f"{README_DIGEST_START}\n"
        "<!-- Auto-generated by digest.py on each run — do not edit by hand. -->\n\n"
        f"{_demote_headings(md_body)}\n\n"
        f"{README_DIGEST_END}"
    )
    README_FILE.write_text(pre + section + post, encoding="utf-8")
    return True


def _badges_html(item: dict) -> str:
    """HTML shields for an item (mirrors :func:`_badges`)."""
    parts: list[str] = []
    aid = _arxiv_id(item.get("link", ""))
    if aid:
        parts.append(
            f'<a href="https://arxiv.org/abs/{escape(aid)}">'
            f'<img src="https://img.shields.io/badge/arXiv-{escape(aid)}-b31b1b.svg" alt="arXiv"></a>'
        )
    elif item.get("link"):
        parts.append(
            f'<a href="{escape(item["link"])}">'
            '<img src="https://img.shields.io/badge/Paper-Link-blue" alt="Paper"></a>'
        )
    site = _website(item.get("summary", ""))
    if site:
        parts.append(
            f'<a href="{escape(site)}">'
            '<img src="https://img.shields.io/badge/Website-Link-blue" alt="Website"></a>'
        )
    return " ".join(parts)


def render_html(items: list[dict], when: datetime, order: list[dict]) -> str:
    parts = [
        '<!doctype html><html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
        'max-width:760px;line-height:1.5;color:#222;">',
        f"<h1>Paper digest — {escape(when.strftime('%Y-%m-%d'))}</h1>",
        f"<p>{len(items)} new papers, generated at "
        f"{escape(when.isoformat())}.</p>",
    ]
    for header, bucket in _sections(items, order):
        parts.append(
            f"<h2>{escape(header)} <small>({len(bucket)})</small></h2>"
        )
        if not bucket:
            parts.append("<p><em>No new entries.</em></p>")
            continue
        parts.append("<ul>")
        for item in bucket:
            title = escape(item.get("title") or "(untitled)")
            model = _model_name(item.get("title") or "")
            head = (
                f"<strong>{escape(model)}</strong>, {title}."
                if model
                else f"<strong>{title}</strong>."
            )
            li = f"<li>{head}"
            badges = _badges_html(item)
            if badges:
                li += f" {badges}"
            authors = _short_authors(item.get("authors", ""))
            if authors:
                li += f" — <em>{escape(authors)}</em>"
            parts.append(li + "</li>")
        parts.append("</ul>")
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

    all_new: list[dict] = []
    filter_cfg = config.get("filter", {}) or {}
    topic_order = _topic_order(filter_cfg)

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
    md_body = render_markdown(all_new, now, topic_order)
    digest_path.write_text(md_body, encoding="utf-8")
    write_github_output("digest_path", str(digest_path.relative_to(ROOT)))
    print(f"[done] wrote {digest_path} ({new_count} new papers)")

    if update_readme(render_compact(all_new, now, topic_order)):
        print("[readme] refreshed Latest digest section")

    try:
        send_email(
            subject=f"[paper digest] {now.strftime('%Y-%m-%d')} — {new_count} new",
            text_body=md_body,
            html_body=render_html(all_new, now, topic_order),
        )
    except Exception as exc:  # noqa: BLE001
        # Email failure shouldn't fail the workflow — digest is already on disk.
        print(f"[email] send failed: {exc!r}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
