"""Paper subscription digest.

Fetch ToC RSS feeds from selected robotics journals, dedupe against
``state.json``, and write a Markdown digest under ``digests/``.

Designed to run unattended in GitHub Actions; safe to re-run locally.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import feedparser

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DIGEST_DIR = ROOT / "digests"
MAX_PER_SOURCE = 500

USER_AGENT = (
    "paper-subscription-bot/1.0 "
    "(+https://github.com/BraveBoBo/paper-subscription)"
)

FEEDS: dict[str, str] = {
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


def entry_key(entry) -> str:
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


def fetch_feed(
    name: str, url: str, seen: Iterable[str]
) -> tuple[list[dict], list[str]]:
    print(f"[fetch] {name}: {url}")
    parsed = feedparser.parse(url, agent=USER_AGENT)
    if parsed.bozo:
        print(f"[warn] {name}: bozo={parsed.bozo_exception!r}")

    seen_set = set(seen)
    new_items: list[dict] = []
    new_keys: list[str] = []
    for entry in parsed.entries:
        key = entry_key(entry)
        if not key or key in seen_set:
            continue
        seen_set.add(key)
        new_keys.append(key)
        new_items.append(
            {
                "source": name,
                "title": (entry.get("title") or "").strip(),
                "link": (entry.get("link") or "").strip(),
                "summary": (entry.get("summary") or "").strip(),
                "published": (
                    entry.get("published") or entry.get("updated") or ""
                ).strip(),
                "authors": _authors(entry),
            }
        )
    print(
        f"[fetch] {name}: {len(new_items)} new / "
        f"{len(parsed.entries)} total"
    )
    return new_items, new_keys


def render_markdown(items: list[dict], when: datetime) -> str:
    lines = [
        f"# Paper digest — {when.strftime('%Y-%m-%d')}",
        "",
        f"Generated at {when.isoformat()} ({len(items)} new papers).",
        "",
    ]
    by_source: dict[str, list[dict]] = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)
    for source in FEEDS:
        bucket = by_source.get(source, [])
        lines.append(f"## {source} ({len(bucket)})")
        lines.append("")
        if not bucket:
            lines.append("_No new entries._")
            lines.append("")
            continue
        for item in bucket:
            title = item["title"] or "(untitled)"
            link = item["link"]
            lines.append(f"### [{title}]({link})" if link else f"### {title}")
            if item["authors"]:
                lines.append(f"*{item['authors']}*")
            if item["published"]:
                lines.append(f"_{item['published']}_")
            if item["summary"]:
                lines.append("")
                lines.append(item["summary"])
            lines.append("")
    return "\n".join(lines)


def write_github_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"{key}={value}\n")


def main() -> int:
    state = load_state()
    all_new: list[dict] = []

    for name, url in FEEDS.items():
        seen = state.get(name, [])
        try:
            new_items, new_keys = fetch_feed(name, url, seen)
        except Exception as exc:  # noqa: BLE001
            print(f"[error] {name}: {exc!r}", file=sys.stderr)
            continue
        if new_keys:
            state[name] = (new_keys + seen)[:MAX_PER_SOURCE]
        all_new.extend(new_items)

    now = datetime.now(timezone.utc)
    new_count = len(all_new)
    write_github_output("new_count", str(new_count))

    save_state(state)

    if new_count == 0:
        print("[done] no new papers")
        write_github_output("digest_path", "")
        return 0

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = DIGEST_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    digest_path.write_text(render_markdown(all_new, now), encoding="utf-8")
    write_github_output("digest_path", str(digest_path.relative_to(ROOT)))
    print(f"[done] wrote {digest_path} ({new_count} new papers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
