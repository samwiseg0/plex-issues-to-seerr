#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""plex_issues: mirror Plex "Report Issue" reports into Seerr issues.

Plex keeps issue reports on plex.tv, never on the server, so this polls the
plex.tv community GraphQL API. Each new report becomes a Seerr issue, filed as
the reporter's Seerr user when a Seerr user's plexUsername matches. The Plex
report then gets a comment linking to the issue. A report that matches nothing
in Seerr goes to Discord. When the Seerr issue is resolved, the Plex report gets
a second comment with the newest staff reply.

Needs Seerr 3.4 or newer. Older Seerr and Overseerr file every issue as the API
key's user. Overseerr's issue list also carries no comments to find a report by.

Stdlib only. Configured by environment variables:
  PLEX_TOKEN           required. plex.tv token of the server owner's account.
  PMS_URL              required. Plex Media Server base URL, e.g. https://plex.example.com
  SEERR_URL            required. Seerr base URL, e.g. https://requests.example.com
  SEERR_API_KEY        required. Seerr API key (Settings, General).
  SEERR_PUBLIC_URL     optional. Seerr URL for the link in the Plex comment. Default: SEERR_URL.
  PLEX_OWNER           optional. plex.tv username the reports are sent to.
                       Default: the account that owns PLEX_TOKEN.
  DISCORD_WEBHOOK_URL  optional. Where unmatched reports go. Empty: each one is
                       logged once at WARNING with reporter, date, text and reason.
  IMPORT_DAYS          optional, default 0. The first run imports this many days back.
  STATE_FILE           optional, default plex_issues_state.json in the working directory.
  POLL_INTERVAL        optional, seconds, 60 to 86400. Set: poll forever and log a failed
                       poll. Unset, empty or 0: poll once and exit non-zero when the poll failed.
  HEARTBEAT_FILE       optional. Deleted at startup and rewritten after every
                       successful poll, so a container healthcheck can test its age.

--dry-run polls once, prints what it would do per report, and writes nothing,
the state and heartbeat files included.

Every poll starts with a read of the PMS /identity and of Seerr /api/v1/auth/me.
A wrong URL or key fails the poll, and the heartbeat goes stale. A report for
another Plex server (its machine id differs) goes to Discord.

Backfill: set IMPORT_DAYS before the first run, and run --dry-run first. Before it
files a report, the poller looks for a Seerr issue with the report's tag. A found
issue rebuilds the report's record, so a lost state file never files a report twice.
A report with no such issue is skipped when it carries a comment that is exactly
"Tracked at <url>/issues/<number>", the poller's own. A comment from anyone else
does not count.

A mirrored report that is no longer in the plex.tv list gets "gone": "not listed"
and is no longer watched. That is how plex.tv shows a deleted report. A later poll
that lists the report again removes the mark.

Accepted trade-offs:
  - A crash between a Discord post and the state save posts the report again.
  - plex.tv pages by the report date in milliseconds. Two reports with the same
    millisecond at a page boundary can be missed, and a mirrored one marked gone
    until a later poll lists it.

Hand edits to the state file. Stop the poller first. A poll rewrites the file.
  - Stop watching a mirrored report that fails every poll: add "gone": true to
    its record under "reports". A poll never removes a true value.
  - Skip a new report that fails every poll: add the stub record
    "<report id>": {"skipped": "by hand"} under "reports". The ERROR line names the id.
  - An empty, corrupt or non-object state file fails every poll. Restore it from
    a backup. Or replace it with {} and set IMPORT_DAYS to cover the gap. Mirrored
    reports then find their Seerr issues by tag and are watched again. Unmatched
    reports post to Discord again.
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REQUIRED_ENV = ("PLEX_TOKEN", "PMS_URL", "SEERR_URL", "SEERR_API_KEY")
URL_ENV = ("PMS_URL", "SEERR_URL", "SEERR_PUBLIC_URL", "DISCORD_WEBHOOK_URL")
USER_AGENT = "plex-issues/1"
PLEX_GQL = "https://community.plex.tv/api"
SEERR_OPEN, SEERR_RESOLVED = 1, 2
TYPE_NAMES = {1: "VIDEO", 2: "AUDIO", 3: "SUBTITLES", 4: "OTHER"}
RESOLVED_DEFAULT = "Resolved. Thanks for the report."
# Ends the first comment of every Seerr issue. A retry finds the issue a crash or a lost
# response left behind by this tag.
REPORT_TAG = "Plex report {}."
TAG_RE = re.compile(r"Plex report \S+\.\Z")
# The poller's own first comment on a Plex report, in full.
TRACKED_RE = re.compile(r"Tracked at \S+/issues/\d+")

# Checked in this order. The first match wins, no match is 4 OTHER.
ISSUE_TYPES = (
    (3, re.compile(r"subtitle|\bsubs?\b|caption", re.I)),
    # \beng catches "English" and its misspellings.
    (2, re.compile(r"audio|sound|language|\beng|dub", re.I)),
    # show/episode/movie/film covers a wrong title or a wrong episode.
    (1, re.compile(r"show|episode|movie|film|skip|play ?back|freez|buffer|video|picture", re.I)),
)

REPORTS_QUERY = """query($cursor: String) {
  reports(first: 50, after: $cursor) {
    nodes { id message url date commentCount
            user { username displayName } recipient { id username } }
    pageInfo { hasNextPage endCursor }
  }
}"""
# first is a literal. plex.tv types it PaginationInt and rejects an Int! variable.
COMMENTS_QUERY = """query($id: ID!) {
  reportComments(id: $id, first: 100) { nodes { message } }
}"""
# The return type is Comment. Confirmed by GraphQL validation errors only, never executed
# during the build.
COMMENT_MUTATION = """mutation($input: CreateReportCommentInput!) {
  createReportComment(input: $input) { id }
}"""


def log(msg):
    print(msg, flush=True)


# --- pure functions (tests/test_plex_issues.py) --------------------------------

def issue_type(text):
    return next((t for t, rx in ISSUE_TYPES if rx.search(text or "")), 4)


def rating_key(url):
    """server://<machineId>/com.plexapp.plugins.library/library/metadata/<ratingKey>"""
    m = re.search(r"/library/metadata/(\d+)", url or "")
    return m.group(1) if m else None


def parse_guids(meta):
    """A PMS Guid array as {"tmdb": "12345", "imdb": "tt0000001", "tvdb": "67890"}.
    Other schemes, plex:// included, are dropped."""
    out = {}
    for g in meta.get("Guid") or []:
        scheme, _, value = str(g.get("id", "")).partition("://")
        if value and scheme in ("tmdb", "imdb", "tvdb"):
            out.setdefault(scheme, value)
    return out


def parse_date(s):
    """An aware datetime. A naive one, e.g. a hand-edited since, reads as UTC."""
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def reporter(report):
    """Returns "Display Name (username)", or the username alone."""
    user = report.get("user") or {}
    name = user.get("username") or "unknown"
    return f"{user['displayName']} ({name})" if user.get("displayName") else name


def seerr_message(report):
    text = (report.get("message") or "").strip() or "(no text)"
    return (f"{text}\n\nReported in Plex by {reporter(report)} on {report['date'][:10]}. "
            + REPORT_TAG.format(report["id"]))


def resolved_reply(issue, reporter_id):
    """The newest comment after the first one that the reporter did not write."""
    comments = sorted(issue.get("comments") or [], key=lambda c: c["id"])[1:]
    replies = [c for c in comments
               if (c.get("user") or {}).get("id") != reporter_id and (c.get("message") or "").strip()]
    return replies[-1]["message"].strip() if replies else RESOLVED_DEFAULT


# --- HTTP --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None  # A 3xx comes back as an error status, so no token follows a redirect.


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(method, url, headers=None, body=None):
    """Returns (status, parsed JSON or None). A network error raises."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=hdrs, data=data)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def plex_gql(env, query, variables):
    status, d = http("POST", PLEX_GQL, {"X-Plex-Token": env["PLEX_TOKEN"]},
                     {"query": query, "variables": variables})
    if status != 200 or not d or d.get("errors"):
        raise RuntimeError(f"plex.tv GraphQL -> HTTP {status}: {str((d or {}).get('errors'))[:300]}")
    return d["data"]


def owner_filter(env):
    """(recipient field, value) that marks a report as sent to the owner. The plex.tv
    uuid of the token's own account doubles as the recipient id."""
    if env["PLEX_OWNER"]:
        return "username", env["PLEX_OWNER"].lower()
    status, d = http("GET", "https://plex.tv/api/v2/user", {"X-Plex-Token": env["PLEX_TOKEN"]})
    if status != 200 or not (d or {}).get("uuid"):
        raise RuntimeError(f"plex.tv /api/v2/user -> HTTP {status}")
    return "id", d["uuid"].lower()


def fetch_reports(env, since):
    """(reports sent to the owner on or after since, newest first, the ids of every
    report read). A report missing from the ids is gone from plex.tv."""
    if "owner" not in env:
        env["owner"] = owner_filter(env)
    field, value = env["owner"]
    out, seen, cursor = [], set(), None
    while True:
        page = plex_gql(env, REPORTS_QUERY, {"cursor": cursor})["reports"]
        for r in page["nodes"]:
            # run() reads the Seerr issue list once per poll, so a repeated node could file twice.
            if r.get("id") in seen:
                continue
            seen.add(r.get("id"))
            try:
                when = parse_date(r["date"])
            except (AttributeError, TypeError, ValueError):
                log(f"WARNING report {r.get('id')}: unreadable date {r.get('date')!r}, skipped")
                continue
            if when < since:
                return out, seen
            if str((r.get("recipient") or {}).get(field) or "").lower() == value:
                out.append(r)
        if not page["pageInfo"]["hasNextPage"]:
            return out, seen
        cursor = page["pageInfo"]["endCursor"]


def report_comments(env, rid):
    return [(n.get("message") or "").strip()
            for n in plex_gql(env, COMMENTS_QUERY, {"id": rid})["reportComments"]["nodes"]]


def comment_once(env, rid, rec, text, save):
    """Posts text on the Plex report. First it saves the text and how many comments on
    the report already equal it. After a crash or a lost response, the next poll counts
    again. One more means the post landed, so it does not post twice."""
    pending = rec.get("comment")
    if not (pending and report_comments(env, rid).count(pending["text"].strip()) > pending["seen"]):
        rec["comment"] = {"text": text, "seen": report_comments(env, rid).count(text.strip())}
        save()
        plex_gql(env, COMMENT_MUTATION, {"input": {"report": rid, "message": text}})
    del rec["comment"]


def pms_item(env, key):
    """PMS metadata for one ratingKey, or None when the item is gone."""
    status, d = http("GET", f"{env['PMS_URL']}/library/metadata/{key}?includeGuids=1",
                     {"X-Plex-Token": env["PLEX_TOKEN"]})
    if status == 404:
        return None
    if status != 200 or not d:
        raise RuntimeError(f"PMS /library/metadata/{key} -> HTTP {status}")
    return d["MediaContainer"]["Metadata"][0]


def seerr(env, method, path, body=None):
    status, d = http(method, env["SEERR_URL"] + path, {"X-Api-Key": env["SEERR_API_KEY"]}, body)
    if status == 200 and d is None:
        # An SSO login page in front of Seerr answers 200 with HTML.
        raise RuntimeError(f"Seerr {method} {path} -> HTTP 200, not JSON")
    return status, d


def seerr_get(env, path, missing_ok=False):
    """The JSON body of a 200. With missing_ok, a 404 returns None."""
    status, d = seerr(env, "GET", path)
    if status == 404 and missing_ok:
        return None
    if status != 200:
        raise RuntimeError(f"Seerr GET {path} -> HTTP {status}")
    return d


def seerr_media_id(env, kind, ids):
    """Seerr's media row id for a movie or tv show, or None when Seerr has none."""
    if "tmdb" in ids:
        d = seerr_get(env, f"/api/v1/{kind}/{ids['tmdb']}", missing_ok=True)
        return ((d or {}).get("mediaInfo") or {}).get("id")
    for scheme in ("imdb", "tvdb"):
        if scheme in ids:
            q = urllib.parse.quote(f"{scheme}:{ids[scheme]}")
            for r in seerr_get(env, f"/api/v1/search?query={q}").get("results", []):
                if r.get("mediaType") == kind and (r.get("mediaInfo") or {}).get("id"):
                    return r["mediaInfo"]["id"]
    return None


def seerr_list(env, path):
    """Every row of a paged Seerr list endpoint. path already carries a "?"."""
    skip = 0
    while True:
        d = seerr_get(env, f"{path}&take=500&skip={skip}")
        yield from d.get("results", [])
        skip += 500
        if skip >= d["pageInfo"]["results"]:
            return


def seerr_users(env):
    """Seerr user id by lowercased plexUsername. Two users whose names differ only by
    case map to neither, so their reports file as the API key's user."""
    # ponytail: matches on plexUsername. A plex.tv rename plus a reused name can misfile a
    # report. Upgrade path: map the plex.tv user id to Seerr's plexId.
    out = {}
    for u in seerr_list(env, "/api/v1/user?"):
        name = (u.get("plexUsername") or "").lower()
        if name in out:
            log(f"WARNING two Seerr users share plexUsername {name!r} up to case. "
                "Their reports file as the API key's user.")
            out[name] = None
        elif name:
            out[name] = u["id"]
    return out


def issue_tags(env):
    """{report tag: Seerr issue id} for every Seerr issue whose first comment ends with a tag."""
    out = {}
    for i in seerr_list(env, "/api/v1/issue?filter=all"):
        if "comments" not in i:
            # Overseerr leaves the comments out. Without them a lost response files the
            # report again, so refuse instead.
            raise RuntimeError("the Seerr issue list carries no comments. plex_issues needs Seerr 3.4 or newer")
        first = min(i["comments"] or [{"id": 0}], key=lambda c: c["id"])
        m = TAG_RE.search((first.get("message") or "").rstrip())
        if m:
            out.setdefault(m.group(0), i["id"])
    return out


def seerr_issue_deleted(env, status, body, issue_id):
    """Seerr answers a deleted issue with HTTP 500 "Issue not found.", the same answer
    as a database error. The issue list tells the two apart."""
    if status == 404:
        return True
    if status != 500 or (body or {}).get("message") != "Issue not found.":
        return False
    return all(i["id"] != issue_id for i in seerr_list(env, "/api/v1/issue?filter=all"))


def resolve(env, report):
    """What the report points at: {title, kind, ids, season, episode}. "why" is set on a no-match."""
    url = report.get("url") or ""
    key = rating_key(url)
    if not key:
        return {"title": None, "why": "the report URL carries no ratingKey"}
    if not url.lower().startswith(f"server://{env['machine'].lower()}/"):
        return {"title": None, "why": "report is for another server"}
    item = pms_item(env, key)
    if item is None:
        return {"title": None, "why": f"ratingKey {key} is gone from the PMS (404)"}
    kind, show, season, episode = item.get("type"), item, 0, 0
    if kind == "episode":
        season, episode = item.get("parentIndex") or 0, item.get("index") or 0
        title = f"{item.get('grandparentTitle')} S{season:02d}E{episode:02d}"
        show = pms_item(env, item["grandparentRatingKey"])
    elif kind == "season":
        season = item.get("index") or 0
        title = f"{item.get('parentTitle')} season {season}"
        show = pms_item(env, item["parentRatingKey"])
    elif kind in ("movie", "show"):
        title = f"{item.get('title')} ({item.get('year')})"
    else:
        return {"title": item.get("title"), "why": f"a {kind} item has no Seerr page"}
    if show is None:
        return {"title": title, "why": "the show is gone from the PMS (404)"}
    ids = parse_guids(show)
    if not ids:
        return {"title": title, "why": "no tmdb/imdb/tvdb Guid on the PMS item"}
    return {"title": title, "kind": "movie" if kind == "movie" else "tv", "ids": ids,
            "season": season, "episode": episode}


# --- Discord ------------------------------------------------------------------------

_CONTROL_RE = re.compile("[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2060\ufeff]")
_MD_RE = re.compile(r"[\\*_~|`\[\]()<>@]")


def md(s):
    return _MD_RE.sub(lambda m: "\\" + m.group(0), _CONTROL_RE.sub(" ", str(s or "")).strip())


def discord_embed(report, match):
    """The Discord embed for a report Seerr cannot take. Discord shows the timestamp
    in the reader's local time."""
    return {
        "title": "Plex issue report not mirrored to Seerr",
        "url": "https://app.plex.tv/desktop/#!/reports",
        "color": 0xFEE75C,
        "description": md(report.get("message"))[:2000] or "(no text)",
        "timestamp": report["date"],
        "footer": {"text": "plex-issues-to-seerr"},
        "fields": [
            {"name": "Reporter", "value": md(reporter(report))[:1024], "inline": True},
            {"name": "Title", "value": md(match.get("title") or "unknown")[:1024], "inline": True},
            {"name": "Why", "value": md(match["why"])[:1024], "inline": False},
        ],
    }


def post_unmatched(env, report, match, head):
    """Posts a report Seerr cannot take to Discord. With no webhook, the WARNING log
    line is the only record of it."""
    why = f"{match['why']}. Title: {match.get('title') or 'unknown'}"
    if not env["DISCORD_WEBHOOK_URL"]:
        log(f"WARNING {head}: not mirrored to Seerr: {why}.")
        return
    status, _ = http("POST", env["DISCORD_WEBHOOK_URL"],
                     body={"embeds": [discord_embed(report, match)], "allowed_mentions": {"parse": []}})
    if status >= 300:
        raise RuntimeError(f"Discord webhook -> HTTP {status}")
    log(f"{head}: not mirrored to Seerr: {why}. Posted to Discord.")


# --- state --------------------------------------------------------------------------

def load_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            state = json.load(f)
    except ValueError:
        state = None
    if not isinstance(state, dict):
        raise RuntimeError(f"state file {path} is empty, corrupt or not a JSON object. "
                           "Refusing to poll. The plex_issues docstring has the recovery.")
    return state


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- the run --------------------------------------------------------------------------

def run(env, state, dry_run, save):
    """Returns True when any step failed. A failed step records nothing final, so the
    next run retries it."""
    failed = False
    records = state.setdefault("reports", {})
    since = parse_date(state["since"])
    users = tags = None
    issue_url = env["SEERR_PUBLIC_URL"] + "/issues/{}"

    # 0. Config health. A wrong URL or key fails the poll here, and the heartbeat goes stale.
    status, d = http("GET", env["PMS_URL"] + "/identity", {"X-Plex-Token": env["PLEX_TOKEN"]})
    env["machine"] = ((d or {}).get("MediaContainer") or {}).get("machineIdentifier")
    if status != 200 or not env["machine"]:
        raise RuntimeError(f"PMS /identity -> HTTP {status}, no machineIdentifier")
    seerr_get(env, "/api/v1/auth/me")

    # 1. New reports, oldest first. A pending record is a POST that may have landed.
    # A failed fetch raises here, so stage 2 never reads a partial list as "gone".
    reports, listed = fetch_reports(env, since)
    for r in reversed(reports):
        rid = r["id"]
        rec = records.get(rid)
        if rec and not rec.get("pending"):
            continue
        name = (r.get("user") or {}).get("username") or ""
        head = f"report {rid} {r['date'][:10]} {reporter(r)} {(r.get('message') or '').strip()!r}"
        try:
            # A crash after the create or a lost state file leaves a Seerr issue with no
            # record. Its tag finds it. The issue list is read once per poll, on first need.
            if tags is None:
                tags = issue_tags(env)
            issue = tags.get(REPORT_TAG.format(rid))
            # Only the poller's own comment counts, the whole comment "Tracked at
            # <url>/issues/<number>". Report text that starts with "Tracked at" does not.
            comments = report_comments(env, rid) if rec is None and r.get("commentCount") else []
            tracked, resolved = any(TRACKED_RE.fullmatch(m) for m in comments), False
            if issue:
                log(f"{head}: Seerr issue {issue} was filed by an earlier poll.")
                if dry_run:
                    continue
                if rec is None:
                    if users is None:
                        users = seerr_users(env)
                    # Goes into records only once it is complete. A later save would write a
                    # half-built record, and no stage would pick the report up again.
                    rec = {"date": r["date"], "reporter": users.get(name.lower())}
                if tracked:
                    # The resolve comment was posted when its text is on the report.
                    d = seerr_get(env, f"/api/v1/issue/{issue}")
                    resolved = (d["status"] == SEERR_RESOLVED
                                and resolved_reply(d, rec.get("reporter")) in comments)
            elif tracked:
                # Mirrored before, but its Seerr issue is deleted or on another Seerr.
                log(f"{head}: has a Tracked at comment and no Seerr issue with its tag, skipped.")
                if not dry_run:
                    records[rid] = {"date": r["date"], "skipped": "already tracked"}
                    save()
                continue
            else:
                match = resolve(env, r)
                media_id = seerr_media_id(env, match["kind"], match["ids"]) if "ids" in match else None
                if "ids" in match and media_id is None:
                    match["why"] = f"Seerr has no media row for {match['kind']} {match['ids']}"
                if media_id is not None:
                    if users is None:
                        users = seerr_users(env)
                    uid = users.get(name.lower())
                    itype = issue_type(r.get("message"))
                    body = {"issueType": itype, "message": seerr_message(r), "mediaId": media_id,
                            "problemSeason": match["season"], "problemEpisode": match["episode"]}
                    if uid:
                        body["userId"] = uid
                    plan = (f"{match['title']}, Seerr media {media_id}, type {itype} {TYPE_NAMES[itype]}, "
                            f"season {match['season']} episode {match['episode']}, "
                            f"as Seerr user {uid or 'of the API key (no plexUsername match)'}")
                    if dry_run:
                        log(f"{head}: {plan}. Would file a Seerr issue and comment on Plex.")
                        continue
                    # Saved before the POST. A crash or a lost response leaves this record,
                    # and the next poll finds the issue by its tag instead of filing twice.
                    rec = records[rid] = {"date": r["date"], "title": match["title"],
                                          "reporter": uid, "pending": True}
                    save()
                    status, d = seerr(env, "POST", "/api/v1/issue", body)
                    if status in (200, 201) and (d or {}).get("id"):
                        issue = d["id"]
                        log(f"{head}: {plan}. Filed Seerr issue {issue}.")
                    elif status in (400, 404, 422):
                        # Seerr refuses this report on every retry. Report it once, move on.
                        # Every other status, 401, 403 and 429 included, is retried.
                        match["why"] = f"Seerr refused the issue: HTTP {status} {(d or {}).get('message')}"
                    else:
                        raise RuntimeError(f"Seerr POST /api/v1/issue -> HTTP {status}: {str(d)[:300]}")
                if not issue:
                    if dry_run:
                        where = "Discord" if env["DISCORD_WEBHOOK_URL"] else "the log (no DISCORD_WEBHOOK_URL)"
                        log(f"{head}: {match['why']}. Would post to {where}.")
                        if env["DISCORD_WEBHOOK_URL"]:
                            log(json.dumps(discord_embed(r, match)))
                        continue
                    post_unmatched(env, r, match, head)
                    records[rid] = {"date": r["date"], "unmatched": match["why"], "title": match["title"]}
                    save()
                    continue
            rec.pop("pending", None)
            rec.update(issue=issue, tracked=tracked, resolved=resolved)
            records[rid] = rec
            save()
        except Exception as e:
            failed = True
            log(f"ERROR {head}: {type(e).__name__}: {e}")

    # 2. Plex comments and Seerr status for every mirrored report.
    # ponytail: re-reads every tracked Seerr issue each poll. Fine at about one report a
    # month. Upgrade path: stop watching issues resolved long ago.
    for rid, rec in records.items():
        if rec.get("gone") == "not listed" and rid in listed:
            # A hand-set "gone": true stays final.
            log(f"report {rid}: listed on plex.tv again. Watched again.")
            del rec["gone"]
            save()
        if "issue" not in rec or rec.get("deleted") or rec.get("gone"):
            continue
        url = issue_url.format(rec["issue"])
        try:
            if rid not in listed and parse_date(rec["date"]) >= since:
                # plex.tv drops a deleted report from the list. A comment on it fails
                # with a generic error, so the list is the only sign.
                log(f"report {rid}: not in the plex.tv list. Not watched until it is listed again.")
                if not dry_run:
                    rec["gone"] = "not listed"
                    save()
                continue
            if not rec.get("tracked"):
                if dry_run:
                    log(f"report {rid}: would comment on Plex: Tracked at {url}")
                    continue
                comment_once(env, rid, rec, f"Tracked at {url}", save)
                rec["tracked"] = True
                save()
                log(f"report {rid}: commented on Plex: Tracked at {url}")
            status, issue = seerr(env, "GET", f"/api/v1/issue/{rec['issue']}")
            if status != 200 and seerr_issue_deleted(env, status, issue, rec["issue"]):
                log(f"report {rid}: Seerr issue {rec['issue']} was deleted. No longer tracked.")
                if not dry_run:
                    rec["deleted"] = True
                    save()
                continue
            if status != 200:
                raise RuntimeError(f"Seerr GET /api/v1/issue/{rec['issue']} -> HTTP {status}")
            if issue["status"] == SEERR_RESOLVED and not rec.get("resolved"):
                reply = resolved_reply(issue, rec.get("reporter"))
                if dry_run:
                    log(f"report {rid}: Seerr issue {rec['issue']} resolved. Would comment on Plex: {reply!r}")
                    continue
                comment_once(env, rid, rec, reply, save)
                rec["resolved"] = True
                save()
                log(f"report {rid}: Seerr issue {rec['issue']} resolved. Commented on Plex.")
            elif issue["status"] == SEERR_OPEN and (rec.get("resolved") or "comment" in rec):
                # A resolve comment left pending by a crash is stale now. Kept, it would
                # count the next identical resolve comment as already posted.
                log(f"report {rid}: Seerr issue {rec['issue']} reopened.")
                if not dry_run:
                    rec["resolved"] = False
                    rec.pop("comment", None)
                    save()
        except Exception as e:
            failed = True
            log(f"ERROR report {rid} (Seerr issue {rec['issue']}): {type(e).__name__}: {e}")
    return failed


def poll(env, path, dry_run):
    """One poll. Returns True when any step failed. The state file is re-read every
    poll, so a hand edit between polls sticks."""
    try:
        state = load_state(path)
        if "since" not in state:
            since = datetime.now(timezone.utc) - timedelta(days=env["IMPORT_DAYS"])
            state["since"] = since.isoformat()
            log(f"first run: importing reports since {since:%Y-%m-%d %H:%M} UTC")

        def save():
            if not dry_run:
                save_state(path, state)

        save()
        return run(env, state, dry_run, save)
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        return True


def main(argv=None):
    p = argparse.ArgumentParser(description="Mirror Plex issue reports into Seerr issues.")
    p.add_argument("--dry-run", action="store_true",
                   help="poll once, print what would happen, write nothing")
    args = p.parse_args(argv)

    # Errors name the variable only, never its value: URLs and keys can carry secrets.
    names = REQUIRED_ENV + ("SEERR_PUBLIC_URL", "DISCORD_WEBHOOK_URL", "PLEX_OWNER",
                            "IMPORT_DAYS", "POLL_INTERVAL")
    env = {k: os.environ.get(k, "").strip() for k in names}
    missing = [k for k in REQUIRED_ENV if not env[k]]
    if missing:
        sys.exit(f"plex_issues: missing env vars: {', '.join(missing)}")
    env["SEERR_PUBLIC_URL"] = env["SEERR_PUBLIC_URL"] or env["SEERR_URL"]
    for k in ("IMPORT_DAYS", "POLL_INTERVAL"):
        if not (env[k] or "0").isdecimal():
            sys.exit(f"plex_issues: {k} must be a whole number")
        env[k] = int(env[k] or 0)
    if env["POLL_INTERVAL"] and not 60 <= env["POLL_INTERVAL"] <= 86400:
        sys.exit("plex_issues: POLL_INTERVAL must be 0 or 60 to 86400 seconds")
    for k in ("PLEX_TOKEN", "SEERR_API_KEY"):
        if not env[k].isprintable() or " " in env[k]:
            sys.exit(f"plex_issues: {k} must not contain spaces or control characters")
    for k in URL_ENV:
        try:
            u = urllib.parse.urlsplit(env[k])
        except ValueError:
            u = None
        if env[k] and not (u and u.scheme in ("http", "https") and u.hostname and "@" not in u.netloc
                           and env[k].isprintable() and " " not in env[k]):
            sys.exit(f"plex_issues: {k} must be an http(s) URL with no user:password and no spaces")
        env[k] = env[k].rstrip("/")
    path = os.environ.get("STATE_FILE") or "plex_issues_state.json"
    heartbeat = os.environ.get("HEARTBEAT_FILE", "")
    interval = 0 if args.dry_run else env["POLL_INTERVAL"]

    if not args.dry_run:
        # A second process started while this one runs backs off instead of
        # filing the same report twice. Held for the life of the process.
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            lock = open(path + ".lock", "w")
        except OSError as e:
            sys.exit(f"plex_issues: cannot create the STATE_FILE directory or lock file: {e.strerror}")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("plex_issues: another run holds the lock, exiting")
            return 0
        if heartbeat:
            # A heartbeat from an earlier container must not pass the healthcheck.
            with contextlib.suppress(FileNotFoundError):
                os.remove(heartbeat)
    # As PID 1 in a container, SIGTERM is ignored without a handler.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))
    log(f"plex_issues: {f'poll every {interval}s' if interval else 'one poll'}, "
        f"owner filter {'PLEX_OWNER' if env['PLEX_OWNER'] else 'token account'}, state {path}, "
        f"Discord {'on' if env['DISCORD_WEBHOOK_URL'] else 'off'}, import days {env['IMPORT_DAYS']}"
        f"{', dry run' if args.dry_run else ''}")

    while True:
        failed = poll(env, path, args.dry_run)
        if args.dry_run:
            log("[dry-run] nothing written, the state and heartbeat files included")
        elif heartbeat and not failed:
            try:
                with open(heartbeat, "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat() + "\n")
            except OSError as e:
                log(f"WARNING heartbeat not written: {type(e).__name__}: {e}")
        if not interval:
            return 1 if failed else 0
        if failed:
            log(f"poll failed, next poll in {interval}s")
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
