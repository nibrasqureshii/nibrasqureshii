"""Refresh the "Recent public activity" section of the GitHub profile README.

Gathers public-only GitHub activity (owned public repos, open-source pull requests,
public events from the last 30 days) and writes it between the ACTIVITY markers in
README.md. Private and client repositories are never read: every endpoint used here
returns public data only. Standard library only, so it costs nothing to run.

Usage:
    python scripts/update_profile.py            # rewrite README.md in place
    python scripts/update_profile.py --dry-run  # print the generated section only

Environment:
    GITHUB_TOKEN    optional locally, set automatically in GitHub Actions (raises rate limits)
    GITHUB_USER     defaults to GITHUB_REPOSITORY_OWNER, then "nibrasqureshii"
    EXCLUDE_REPOS   comma-separated repos never to show (kept in a repo variable, not in code):
                    "name" hides one of my repos, "owner/name" hides any repo (e.g. PR targets)
"""
import argparse
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("update_profile")

API = "https://api.github.com"
README = Path(__file__).resolve().parent.parent / "README.md"
START, END = "<!-- ACTIVITY:START -->", "<!-- ACTIVITY:END -->"
WINDOW_DAYS = 30


def github_get(path, params=None):
    """GET a GitHub REST endpoint and return parsed JSON."""
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "profile-activity-updater",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def gather(user, excluded):
    """Collect public activity facts, skipping the profile repo and excluded repos."""
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    # Bare names are my own repos; "owner/name" entries can hide anyone's repo
    hidden = {f"{user}/{user}".lower()} | {
        (name if "/" in name else f"{user}/{name}").lower() for name in excluded
    }

    # Owned, public, non-fork repos, most recently pushed first
    repos = [
        {"name": r["name"], "url": r["html_url"], "description": r["description"] or "",
         "language": r["language"] or "", "pushed_at": r["pushed_at"]}
        for r in github_get(f"/users/{user}/repos", {"type": "owner", "sort": "pushed", "per_page": 100})
        if not r["fork"] and not r["private"] and r["full_name"].lower() not in hidden
    ][:5]

    # Pull requests opened against other people's public repositories
    found = github_get("/search/issues", {
        "q": f"is:pr author:{user} is:public -user:{user}", "sort": "updated", "per_page": 10,
    })
    prs = [{
        "title": item["title"],
        "repo": item["repository_url"].split("/repos/", 1)[1],
        "url": item["html_url"],
        "state": "merged" if (item.get("pull_request") or {}).get("merged_at") else item["state"],
    } for item in found.get("items", [])
        if item["repository_url"].split("/repos/", 1)[1].lower() not in hidden]

    # Public events in the window (the API keeps ~90 days, max 300 events)
    counts = Counter()
    for page in (1, 2, 3):
        events = github_get(f"/users/{user}/events/public", {"per_page": 100, "page": page})
        for ev in events:
            created = datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
            if created >= since and ev["repo"]["name"].lower() not in hidden:
                counts[ev["type"]] += 1
        if len(events) < 100:
            break

    return {"recent_repos": repos, "open_source_prs": prs, "event_counts": counts}


def render(facts):
    """Build the markdown that goes between the README markers."""
    lines = []
    if facts["recent_repos"]:
        lines.append("**Recently updated public repos**")
        for r in facts["recent_repos"]:
            meta = " · ".join(x for x in (r["language"], f"updated {r['pushed_at'][:10]}") if x)
            desc = f": {r['description']}" if r["description"] else ""
            lines.append(f"- [{r['name']}]({r['url']}){desc} <sub>({meta})</sub>")
        lines.append("")

    if facts["open_source_prs"]:
        lines.append("**Open-source pull requests**")
        for pr in facts["open_source_prs"][:5]:
            lines.append(f"- [{pr['title']}]({pr['url']}) in `{pr['repo']}` ({pr['state']})")
        lines.append("")

    c = facts["event_counts"]
    stats = [f"{c[k]} {one if c[k] == 1 else many}" for k, one, many in (
        ("PushEvent", "push", "pushes"),
        ("PullRequestEvent", "pull request update", "pull request updates"),
        ("CreateEvent", "new repo or branch", "new repos or branches"),
        ("ReleaseEvent", "release", "releases"),
    ) if c.get(k)]
    summary = ", ".join(stats) if stats else "quiet in public; most of my work is in private client repos"
    lines.append(f"<sub>Public activity, last {WINDOW_DAYS} days: {summary}. "
                 f"Updated {datetime.now(timezone.utc):%Y-%m-%d} by a GitHub Action.</sub>")
    return "\n".join(lines)


def strip_dates(block):
    """Drop the volatile date so unchanged activity doesn't cause a commit."""
    return re.sub(r"Updated \d{4}-\d{2}-\d{2}", "", block)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the section instead of writing README.md")
    args = parser.parse_args()

    user = os.environ.get("GITHUB_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER") or "nibrasqureshii"
    excluded = [n.strip() for n in os.environ.get("EXCLUDE_REPOS", "").split(",") if n.strip()]
    try:
        facts = gather(user, excluded)
    except Exception:
        log.exception("Failed to gather GitHub activity")
        return 1

    block = render(facts)
    if args.dry_run:
        print(block)
        return 0

    readme = README.read_text()
    if START not in readme or END not in readme:
        log.error("Markers %s / %s not found in %s", START, END, README)
        return 1
    head, rest = readme.split(START, 1)
    old_block, tail = rest.split(END, 1)
    if strip_dates(old_block.strip()) == strip_dates(block.strip()):
        log.info("No change in public activity; README left as is")
        return 0
    README.write_text(f"{head}{START}\n{block}\n{END}{tail}")
    log.info("README activity section updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
