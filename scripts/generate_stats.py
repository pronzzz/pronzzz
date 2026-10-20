#!/usr/bin/env python3
"""Render github-metrics.svg and languages.svg for the profile README.

Replaces lowlighter/metrics, which only ever read the first page of repositories
when the page size was below 100 (and GitHub 502s on a page of 100 for this account).
Uses only the standard library. Needs GH_TOKEN with `repo` scope to include private repos.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

OWNER = os.environ.get("GITHUB_REPOSITORY_OWNER", "pronzzz")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
ROOT = Path(__file__).resolve().parent.parent
LINES_CACHE = ROOT / ".github" / "stats" / "lines-cache.json"

# Weeks where a repo gains or loses more lines than this are bulk commits of vendored or
# generated files (committed virtualenvs, build output, datasets), not hand-written code
LINES_BULK_WEEK = 50_000
# C#, GLSL and C only come from vendored upstream code in a private emulator repo;
# notebook bytes are mostly embedded output images, not code
LANGUAGES_IGNORED = {"C#", "GLSL", "C", "Jupyter Notebook"}
LANGUAGES_LIMIT = 8
PAGE_SIZE = 25

QUERY = """
query($login: String!, $after: String, $first: Int!) {
  user(login: $login) {
    name
    createdAt
    followers { totalCount }
    contributionsCollection {
      totalCommitContributions
      restrictedContributionsCount
      totalPullRequestContributions
      contributionCalendar { totalContributions }
    }
    repositories(first: $first, after: $after, ownerAffiliations: OWNER, isFork: false,
                 orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        stargazerCount
        forkCount
        releases { totalCount }
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def request(url, body=None, attempts=4):
    """Return (status, parsed JSON or None); retry transient 5xx and network errors."""
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
               "User-Agent": f"{OWNER}-profile-stats"}
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=60) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == attempts - 1:
                raise
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(5 * (attempt + 1))


def fetch_profile():
    """Page through every owned, non-fork repository; fail rather than return a partial set."""
    user, repos, after = None, [], None
    while True:
        _, result = request("https://api.github.com/graphql", {
            "query": QUERY, "variables": {"login": OWNER, "after": after, "first": PAGE_SIZE}})
        if result.get("errors"):
            sys.exit(f"GraphQL error: {result['errors']}")
        user = result["data"]["user"]
        page = user["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    if len(repos) != user["repositories"]["totalCount"]:
        sys.exit(f"Fetched {len(repos)} of {user['repositories']['totalCount']} repositories, refusing to render")
    return user, repos


def fetch_lines(repos):
    """Sum lines added/removed by OWNER from contributor stats, reusing cached values while GitHub computes them."""
    cache = json.loads(LINES_CACHE.read_text()) if LINES_CACHE.exists() else {}
    pending = [r["name"] for r in repos]
    for attempt in range(4):
        still_computing = []
        for name in pending:
            try:
                status, stats = request(f"https://api.github.com/repos/{OWNER}/{name}/stats/contributors")
            except urllib.error.HTTPError as error:  # e.g. 409 for an empty repository
                print(f"Skipping lines for {name}: HTTP {error.code}")
                continue
            if status == 202:
                still_computing.append(name)
                continue
            mine = [c for c in stats or [] if (c.get("author") or {}).get("login", "").lower() == OWNER.lower()]
            weeks = [w for c in mine for w in c["weeks"] if max(w["a"], w["d"]) <= LINES_BULK_WEEK]
            cache[name] = {"added": sum(w["a"] for w in weeks), "removed": sum(w["d"] for w in weeks)}
        pending = still_computing
        if not pending:
            break
        time.sleep(20)
    if pending:
        print(f"Contributor stats still computing for {len(pending)} repos, using cached values: {', '.join(pending)}")
    current = {r["name"] for r in repos}
    cache = {name: value for name, value in sorted(cache.items()) if name in current}
    LINES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LINES_CACHE.write_text(json.dumps(cache, indent=2) + "\n")
    return sum(v["added"] for v in cache.values()), sum(v["removed"] for v in cache.values())


def compact(number):
    for divisor, suffix in ((1_000_000, "M"), (1_000, "k")):
        if number >= divisor:
            return f"{number / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(number)


def size(nbytes):
    for divisor, suffix in ((1024 ** 2, "MB"), (1024, "kB")):
        if nbytes >= divisor:
            return f"{nbytes / divisor:.1f} {suffix}"
    return f"{nbytes} B"


STYLE = """
  <style>
    .card { fill: #ffffff; stroke: #d0d7de; }
    .title { font: 600 17px 'Segoe UI', Ubuntu, Helvetica, Arial, sans-serif; fill: #2196F3; }
    .sub { font: 400 12px 'Segoe UI', Ubuntu, Helvetica, Arial, sans-serif; fill: #57606a; }
    .label { font: 400 13px 'Segoe UI', Ubuntu, Helvetica, Arial, sans-serif; fill: #57606a; }
    .value { font: 600 14px 'Segoe UI', Ubuntu, Helvetica, Arial, sans-serif; fill: #1f2328; }
    .rule { stroke: #d8dee4; }
    @media (prefers-color-scheme: dark) {
      .card { fill: #0d1117; stroke: #30363d; }
      .sub, .label { fill: #8d96a0; }
      .value { fill: #e6edf3; }
      .rule { stroke: #21262d; }
    }
  </style>"""

WIDTH = 480


def stats_svg(user, repos, lines):
    years = (datetime.now(timezone.utc) - datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00"))).days // 365
    contributions = user["contributionsCollection"]
    rows = [
        ("Stars earned", compact(sum(r["stargazerCount"] for r in repos))),
        ("Repositories", compact(len(repos))),
        ("Commits (past year)", compact(contributions["totalCommitContributions"] + contributions["restrictedContributionsCount"])),
        ("Releases", compact(sum(r["releases"]["totalCount"] for r in repos))),
        ("Pull requests (past year)", compact(contributions["totalPullRequestContributions"])),
        ("Forks", compact(sum(r["forkCount"] for r in repos))),
        ("Lines added", compact(lines[0])),
        ("Lines removed", compact(lines[1])),
        ("Contributions (past year)", compact(contributions["contributionCalendar"]["totalContributions"])),
        ("Followers", compact(user["followers"]["totalCount"])),
    ]
    row_height, top = 30, 78
    height = top + (len(rows) // 2) * row_height + 10
    column = (WIDTH - 50) // 2
    body = []
    for index, (label, value) in enumerate(rows):
        x = 25 + (index % 2) * (column + 10)
        y = top + (index // 2) * row_height
        body.append(f'<text class="label" x="{x}" y="{y}">{escape(label)}</text>'
                    f'<text class="value" x="{x + column - 10}" y="{y}" text-anchor="end">{escape(value)}</text>'
                    f'<line class="rule" x1="{x}" x2="{x + column - 10}" y1="{y + 10}" y2="{y + 10}"/>')
    subtitle = f"Joined GitHub {years} years ago"
    return height, f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="GitHub stats for {escape(user['name'] or OWNER)}">{STYLE}
  <rect class="card" x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="10"/>
  <text class="title" x="25" y="34">{escape(user['name'] or OWNER)}'s GitHub Stats</text>
  <text class="sub" x="25" y="52">{escape(subtitle)}</text>
  {''.join(body)}
</svg>
"""


def languages_svg(repos, min_height):
    totals, colors = {}, {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in LANGUAGES_IGNORED:
                continue
            totals[name] = totals.get(name, 0) + edge["size"]
            colors[name] = edge["node"]["color"] or "#8d96a0"
    ranked = sorted(totals.items(), key=lambda item: -item[1])[:LANGUAGES_LIMIT]
    shown = sum(value for _, value in ranked) or 1
    bar_width, bar_x, bar_y = WIDTH - 50, 25, 66
    segments, x = [], float(bar_x)
    for name, value in ranked:
        width = bar_width * value / shown
        segments.append(f'<rect x="{x:.2f}" y="{bar_y}" width="{width:.2f}" height="10" fill="{colors[name]}"/>')
        x += width
    legend, column = [], (WIDTH - 50) // 2
    for index, (name, value) in enumerate(ranked):
        lx = 25 + (index % 2) * (column + 10)
        ly = 104 + (index // 2) * 26
        legend.append(f'<circle cx="{lx + 5}" cy="{ly - 4}" r="5" fill="{colors[name]}"/>'
                      f'<text class="value" x="{lx + 16}" y="{ly}">{escape(name)}</text>'
                      f'<text class="label" x="{lx + column - 10}" y="{ly}" text-anchor="end">{value / shown:.1%} · {size(value)}</text>')
    # Match the stats card so the two sit evenly side by side
    height = max(104 + ((len(ranked) + 1) // 2) * 26, min_height)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="Most used languages">{STYLE}
  <rect class="card" x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="10"/>
  <text class="title" x="25" y="34">Most Used Languages</text>
  <text class="sub" x="25" y="52">By bytes of code across {len(repos)} repositories</text>
  <clipPath id="bar"><rect x="{bar_x}" y="{bar_y}" width="{bar_width}" height="10" rx="5"/></clipPath>
  <g clip-path="url(#bar)">{''.join(segments)}</g>
  {''.join(legend)}
</svg>
"""


def main():
    if not TOKEN:
        sys.exit("Set GH_TOKEN to a token with repo scope")
    user, repos = fetch_profile()
    lines = fetch_lines(repos)
    stats_height, stats = stats_svg(user, repos, lines)
    (ROOT / "github-metrics.svg").write_text(stats)
    (ROOT / "languages.svg").write_text(languages_svg(repos, stats_height))
    print(f"Rendered stats for {len(repos)} repositories: +{lines[0]} / -{lines[1]} lines")


if __name__ == "__main__":
    main()
