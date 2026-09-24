# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plex_issues.py.

No network. The run() tests use an in-memory plex.tv, PMS and Seerr.
Every sample text is invented.
Run: pytest tests/test_plex_issues.py
"""
import importlib.util
import json
import os

import pytest

SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "..", "plex_issues.py")
_spec = importlib.util.spec_from_file_location("plex_issues", SCRIPT_PATH)
pi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi)


# 3 SUBTITLES, 2 AUDIO, 1 VIDEO, 4 OTHER.
@pytest.mark.parametrize("text,expected", [
    ("the captions drift after an hour", 3),
    ("subs and the dub are both out of sync", 3),   # subtitles beat audio
    ("voices sound like a tin can", 2),
    ("englsh track missing", 2),                    # \beng catches misspellings
    ("picture stutters and the audio lags", 2),     # audio beats video
    ("picture freezes near the end", 1),
    ("the whole thing is purple", 4),
    ("", 4),
    (None, 4),
])
def test_issue_type(text, expected):
    assert pi.issue_type(text) == expected


def test_rating_key():
    url = "server://0123456789abcdef/com.plexapp.plugins.library/library/metadata/4242"
    assert pi.rating_key(url) == "4242"
    assert pi.rating_key("server://0123456789abcdef/com.plexapp.plugins.library") is None
    assert pi.rating_key(None) is None


def test_parse_guids():
    meta = {"Guid": [{"id": "imdb://tt0000001"}, {"id": "tmdb://101"}, {"id": "tvdb://202"},
                     {"id": "tmdb://999"}]}
    assert pi.parse_guids(meta) == {"imdb": "tt0000001", "tmdb": "101", "tvdb": "202"}
    assert pi.parse_guids({}) == {}
    assert pi.parse_guids({"Guid": [{"id": "plex://movie/abc"}, {"id": "junk"}]}) == {}


def test_parse_date_naive_is_utc():
    assert pi.parse_date("2026-01-02").tzinfo is not None
    assert pi.parse_date("2026-01-02T03:04:05.678Z") > pi.parse_date("2026-01-02")


def test_resolved_reply():
    issue = {"comments": [
        {"id": 41, "user": {"id": 1}, "message": "Swapped in a new copy."},
        {"id": 40, "user": {"id": 9}, "message": "the file will not open"},
        {"id": 42, "user": {"id": 9}, "message": "cheers"},
    ]}
    assert pi.resolved_reply(issue, 9) == "Swapped in a new copy."
    # Unmapped reporter: the issue was filed as the API key's user, so every later comment counts.
    assert pi.resolved_reply(issue, None) == "cheers"
    assert pi.resolved_reply({"comments": issue["comments"][1:2]}, 9) == pi.RESOLVED_DEFAULT


def test_seerr_message():
    r = {"id": "abc-123", "message": "  ", "date": "2026-01-02T03:04:05.678Z",
         "user": {"username": "alice01", "displayName": "Alice Example"}}
    assert pi.seerr_message(r) == ("(no text)\n\nReported in Plex by Alice Example (alice01) "
                                   "on 2026-01-02. Plex report abc-123.")
    r["user"] = {"username": None}
    assert pi.seerr_message(r).endswith("by unknown on 2026-01-02. Plex report abc-123.")


def test_md():
    assert pi.md("@everyone look") == "\\@everyone look"
    assert pi.md("<@123>") == "\\<\\@123\\>"
    assert pi.md("*b* _i_ `c` [x](y) ~s~ |p|") == \
        "\\*b\\* \\_i\\_ \\`c\\` \\[x\\]\\(y\\) \\~s\\~ \\|p\\|"
    assert pi.md("\u200b\n\t") == ""


def test_discord_embed():
    r = {"id": "abc-123", "message": "\u200b", "date": "2026-01-02T03:04:05.678Z",
         "user": {"username": "alice01"}}
    e = pi.discord_embed(r, {"title": None, "why": "report is for another server"})
    assert e["description"] == "(no text)"
    assert e["timestamp"] == "2026-01-02T03:04:05.678Z"
    assert e["footer"] == {"text": "plex-issues-to-seerr"}
    assert e["url"] == "https://app.plex.tv/desktop/#!/reports"
    assert [f["value"] for f in e["fields"]] == ["alice01", "unknown", "report is for another server"]


ITEMS = {
    "10": {"type": "movie", "title": "Invented Film", "year": 2001, "Guid": [{"id": "tmdb://900"}]},
    "20": {"type": "show", "title": "Invented Show", "year": 2010,
           "Guid": [{"id": "tvdb://800"}, {"id": "plex://show/x"}]},
    "21": {"type": "season", "index": 3, "parentTitle": "Invented Show", "parentRatingKey": "20"},
    "22": {"type": "episode", "parentIndex": 2, "index": 5, "grandparentTitle": "Invented Show",
           "grandparentRatingKey": "20", "Guid": [{"id": "tvdb://777"}]},
    "30": {"type": "track", "title": "Invented Song"},
    "40": {"type": "movie", "title": "Plex Only", "year": 2002, "Guid": [{"id": "plex://movie/y"}]},
}


@pytest.mark.parametrize("key,want", [
    ("10", {"title": "Invented Film (2001)", "kind": "movie", "ids": {"tmdb": "900"},
            "season": 0, "episode": 0}),
    ("21", {"title": "Invented Show season 3", "kind": "tv", "ids": {"tvdb": "800"},
            "season": 3, "episode": 0}),
    # An episode uses the show's Guids, never its own.
    ("22", {"title": "Invented Show S02E05", "kind": "tv", "ids": {"tvdb": "800"},
            "season": 2, "episode": 5}),
    ("30", {"title": "Invented Song", "why": "a track item has no Seerr page"}),
    ("40", {"title": "Plex Only (2002)", "why": "no tmdb/imdb/tvdb Guid on the PMS item"}),
])
def test_resolve(monkeypatch, key, want):
    monkeypatch.setattr(pi, "pms_item", lambda env, k: ITEMS.get(k))
    url = f"server://abc123/com.plexapp.plugins.library/library/metadata/{key}"
    assert pi.resolve({"machine": "abc123"}, {"url": url}) == want


def test_resolve_other_server(monkeypatch):
    monkeypatch.setattr(pi, "pms_item", lambda env, k: ITEMS.get(k))
    url = "server://def456/com.plexapp.plugins.library/library/metadata/10"
    assert pi.resolve({"machine": "abc123"}, {"url": url})["why"] == "report is for another server"


def test_issue_tags(monkeypatch):
    rows = [{"id": 7, "comments": [{"id": 2, "message": "later. Plex report abc-123."},
                                   {"id": 1, "message": "text. Plex report abc-123.\n"}]},
            {"id": 8, "comments": [{"id": 3, "message": "text. Plex report abc-1234."}]},
            {"id": 9, "comments": [{"id": 4, "message": "no tag"}]},
            {"id": 10, "comments": []}]
    monkeypatch.setattr(pi, "seerr_list", lambda env, path: iter(rows))
    tags = pi.issue_tags({})
    assert tags == {"Plex report abc-123.": 7, "Plex report abc-1234.": 8}
    assert tags.get(pi.REPORT_TAG.format("abc-12")) is None
    # Overseerr leaves the comments out of the list. Refuse, never file again silently.
    rows.append({"id": 11})
    with pytest.raises(RuntimeError, match="Seerr 3.4 or newer"):
        pi.issue_tags({})


def test_comment_once(monkeypatch):
    """A fake plex.tv. lose=True commits the comment, then raises like a lost response."""
    comments, lose = [], [False]

    def gql(env, query, variables):
        if "createReportComment" in query:
            comments.append(variables["input"]["message"])
            if lose[0]:
                raise RuntimeError("connection reset")
            return {}
        return {"reportComments": {"nodes": [{"message": m} for m in comments]}}

    monkeypatch.setattr(pi, "plex_gql", gql)
    rec = {}
    pi.comment_once({}, "r", rec, "Resolved.", lambda: None)
    assert comments == ["Resolved."] and rec == {}
    # A lost response leaves the pending text. The retry sees it landed and posts nothing.
    lose[0] = True
    with pytest.raises(RuntimeError):
        pi.comment_once({}, "r", rec, "Resolved.", lambda: None)
    assert rec["comment"] == {"text": "Resolved.", "seen": 1}
    lose[0] = False
    pi.comment_once({}, "r", rec, "Resolved.", lambda: None)
    assert comments == ["Resolved."] * 2 and rec == {}
    # A crash before the post: the older identical comment does not count as this one.
    rec["comment"] = {"text": "Resolved.", "seen": 2}
    pi.comment_once({}, "r", rec, "Resolved.", lambda: None)
    assert comments == ["Resolved."] * 3 and rec == {}


# --- run() against an in-memory plex.tv, PMS and Seerr -----------------------------

DATE = "2026-01-02T03:04:05.678Z"
ENV = {"PMS_URL": "http://pms", "PLEX_TOKEN": "t", "SEERR_URL": "http://seerr",
       "SEERR_PUBLIC_URL": "https://seerr.example.com", "DISCORD_WEBHOOK_URL": ""}
TRACKED = "Tracked at https://seerr.example.com/issues/42"


class World:
    def __init__(self):
        self.reports, self.listed, self.comments, self.issues = [], None, {}, {}
        self.fail_once = set()  # Seerr paths that answer 503 once

    def report(self, rid):
        self.reports.append({"id": rid, "date": DATE, "message": "the picture drops",
                             "url": "", "user": {"username": "alice01"}})

    def issue(self, iid, rid, status=1):
        self.issues[iid] = {"id": iid, "status": status, "comments": [
            {"id": 1, "user": {"id": 38}, "message": "text. " + pi.REPORT_TAG.format(rid)}]}

    def poll(self, state):
        state.setdefault("since", "2026-01-01T00:00:00+00:00")
        assert not pi.run(dict(ENV), state, False, lambda: None)
        return state


@pytest.fixture
def world(monkeypatch):
    w = World()

    def seerr(env, method, path, body=None):
        if path in w.fail_once:
            w.fail_once.remove(path)
            return 503, None
        if path == "/api/v1/auth/me":
            return 200, {}
        if path.startswith("/api/v1/issue?"):
            return 200, {"pageInfo": {"results": len(w.issues)}, "results": list(w.issues.values())}
        if path.startswith("/api/v1/user?"):
            return 200, {"pageInfo": {"results": 1}, "results": [{"id": 38, "plexUsername": "Alice01"}]}
        iid = int(path.rsplit("/", 1)[1])
        return (200, w.issues[iid]) if iid in w.issues else (500, {"message": "Issue not found."})

    def gql(env, query, variables):
        if "createReportComment" in query:
            w.comments.setdefault(variables["input"]["report"], []).append(variables["input"]["message"])
            return {}
        return {"reportComments": {"nodes": [{"message": m} for m in w.comments.get(variables["id"], [])]}}

    def fetch_reports(env, since):
        nodes = [dict(r, commentCount=len(w.comments.get(r["id"], []))) for r in w.reports]
        return nodes, {r["id"] for r in w.reports} if w.listed is None else w.listed

    monkeypatch.setattr(pi, "http", lambda *a, **k: (200, {"MediaContainer": {"machineIdentifier": "m"}}))
    monkeypatch.setattr(pi, "seerr", seerr)
    monkeypatch.setattr(pi, "plex_gql", gql)
    monkeypatch.setattr(pi, "fetch_reports", fetch_reports)
    monkeypatch.setattr(pi, "resolve", lambda env, r: {"title": None, "why": "no match in this test"})
    return w


def test_only_the_pollers_own_tracked_comment_skips(world):
    world.report("r-1")
    world.report("r-2")
    world.comments = {"r-1": ["Tracked at 12 minutes the picture drops"], "r-2": [TRACKED]}
    recs = world.poll({})["reports"]
    assert recs["r-1"]["unmatched"] == "no match in this test"
    assert recs["r-2"]["skipped"] == "already tracked"


def test_lost_state_rebuilds_the_record(world):
    world.report("r-1")
    world.issue(42, "r-1")
    world.comments["r-1"] = [TRACKED]
    state = world.poll({})
    assert state["reports"]["r-1"] == {"date": DATE, "reporter": 38, "issue": 42,
                                       "tracked": True, "resolved": False}
    assert world.comments["r-1"] == [TRACKED]
    # The rebuilt record is watched. A resolve comments once.
    world.issues[42]["status"] = pi.SEERR_RESOLVED
    world.poll(state)
    assert world.comments["r-1"] == [TRACKED, pi.RESOLVED_DEFAULT]
    # Lost again after the resolve comment: rebuilt as resolved, no second comment.
    state = world.poll({})
    assert state["reports"]["r-1"]["resolved"] is True
    assert world.comments["r-1"] == [TRACKED, pi.RESOLVED_DEFAULT]


def test_failed_rebuild_leaves_no_record(world):
    """A 503 during a rebuild leaves no half-built record for a later save to write."""
    world.report("r-2")  # Unmatched. Runs after r-1, and its save writes the whole state.
    world.report("r-1")
    world.issue(42, "r-1", status=pi.SEERR_RESOLVED)
    world.comments["r-1"] = [TRACKED]
    world.fail_once.add("/api/v1/issue/42")
    disk = {}
    state = {"since": "2026-01-01T00:00:00+00:00"}
    assert pi.run(dict(ENV), state, False, lambda: disk.update(json.loads(json.dumps(state))))
    assert "r-1" not in disk["reports"]
    # The next poll reads the file, rebuilds the record and posts the resolve comment once.
    state = world.poll(disk)
    assert state["reports"]["r-1"]["issue"] == 42
    assert world.comments["r-1"] == [TRACKED, pi.RESOLVED_DEFAULT]
    world.poll(state)
    assert world.comments["r-1"] == [TRACKED, pi.RESOLVED_DEFAULT]


def test_gone_not_listed_comes_back(world):
    world.report("r-1")
    world.issue(42, "r-1", status=pi.SEERR_RESOLVED)
    state = {"reports": {"r-1": {"date": DATE, "reporter": 38, "issue": 42,
                                 "tracked": True, "resolved": False}}}
    world.listed = set()
    world.poll(state)
    assert state["reports"]["r-1"]["gone"] == "not listed"
    assert "r-1" not in world.comments
    # Listed again: the mark goes and the resolve comment posts.
    world.listed = None
    world.poll(state)
    assert "gone" not in state["reports"]["r-1"]
    assert world.comments["r-1"] == [pi.RESOLVED_DEFAULT]
    # A hand-set true stays.
    state["reports"]["r-1"].update(gone=True, resolved=False)
    world.poll(state)
    assert state["reports"]["r-1"]["gone"] is True
    assert world.comments["r-1"] == [pi.RESOLVED_DEFAULT]
