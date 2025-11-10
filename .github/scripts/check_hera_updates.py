#!/usr/bin/env python3
import os, json, datetime, sys
from urllib.parse import urlparse
import feedparser

STATE_FILE = os.environ.get("STATE_FILE", ".hera_state.json")
FEED_URL = os.environ.get("FEED_URL", "https://health.ec.europa.eu/node/13269/rss_en")
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"

def load_state(path):
    if not os.path.exists(path):
        return {"ids": [], "last_checked_iso": None}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch_entries(feed_url):
    d = feedparser.parse(feed_url)
    if d.bozo:
        print(f"Warning: issue reading feed: {d.bozo_exception}", file=sys.stderr)
    entries = []
    for e in d.entries:
        eid = e.get("id") or e.get("link")
        if not eid:
            # Build a deterministic ID from link or title
            eid = (e.get("link") or e.get("title") or "").strip()
        published = e.get("published") or e.get("updated") or ""
        # Normalize
        entries.append({
            "id": eid.strip(),
            "title": (e.get("title") or "").strip(),
            "link": (e.get("link") or "").strip(),
            "published": published.strip()
        })
    return entries

def write_output(name, value):
    # Supports multiline values via <<EOF blocks
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    if "\n" in value:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}<<EOF\n{value}\nEOF\n")
    else:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")

def main():
    now = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    state = load_state(STATE_FILE)
    prev_ids = set(state.get("ids", []))

    entries = fetch_entries(FEED_URL)
    # Keep most-recent-first (feed usually already is)
    # Safety: cap stored IDs to avoid unbounded growth
    MAX_STORE = 200

    # Identify new entries by id
    new_entries = [e for e in entries if e["id"] and e["id"] not in prev_ids]

    # Prepare email if needed
    has_updates = bool(new_entries) and (bool(prev_ids) or NOTIFY_ON_FIRST_RUN)

    # Update state regardless (to move first-run forward)
    # Store a rolling window of seen IDs from the current feed + previous
    merged_ids = [e["id"] for e in entries] + [i for i in state.get("ids", []) if i not in {e["id"] for e in entries}]
    merged_ids = merged_ids[:MAX_STORE]
    new_state_changed = merged_ids != state.get("ids", [])
    state["ids"] = merged_ids
    state["last_checked_iso"] = now
    save_state(STATE_FILE, state)

    # Compose subject/body
    today = datetime.datetime.utcnow().date().isoformat()
    subject = f"HERA latest updates: {len(new_entries)} new item(s) as of {today}"
    if new_entries:
        body_lines = [
            f"Found {len(new_entries)} new item(s) in the HERA 'Latest updates' feed:",
            "",
        ]
        for e in new_entries:
            line = f"- {e['title']} ({e['published']})\n  {e['link']}"
            body_lines.append(line)
        body_lines.append("")
        body_lines.append(f"Feed: {FEED_URL}")
    else:
        body_lines = [
            "No new items since last check.",
            f"Feed: {FEED_URL}"
        ]
    body = "\n".join(body_lines)

    # Write job outputs
    write_output("has_updates", "true" if has_updates else "false")
    write_output("state_changed", "true" if new_state_changed else "false")
    write_output("email_subject", subject)
    write_output("email_body", body)

    # Also log something useful
    print(subject)
    if new_entries:
        for e in new_entries:
            print(f"NEW: {e['title']}  -> {e['link']}")

if __name__ == "__main__":
    main()
