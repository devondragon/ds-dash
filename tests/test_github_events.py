"""_summarize_event + _repo_short — the public-events feed shaping."""
from daemon import _repo_short, _summarize_event


def test_repo_short():
    assert _repo_short("https://api.github.com/repos/owner/name") == "owner/name"
    assert _repo_short("") == ""


def _push_event(**payload_overrides):
    payload = {
        "ref": "refs/heads/main",
        "head": "beef" * 10,
        "before": "cafe" * 10,
        "commits": [
            {"distinct": True, "message": "fix: one\n\nlong body"},
            {"distinct": True, "message": "feat: two"},
        ],
    }
    payload.update(payload_overrides)
    return {
        "type": "PushEvent",
        "repo": {"name": "o/r"},
        "payload": payload,
        "created_at": "2026-06-01T12:00:00Z",
    }


def test_push_event_headline_branch_and_compare_url():
    out = _summarize_event(_push_event())
    assert out["kind"] == "push"
    assert out["repo"] == "o/r"
    # newest distinct commit subject, target branch, +N for the rest
    assert out["detail"] == "feat: two → main +1"
    assert out["url"] == "https://github.com/o/r/compare/" + "cafe" * 10 + "..." + "beef" * 10


def test_push_event_with_no_distinct_commits_is_dropped():
    e = _push_event(commits=[{"distinct": False, "message": "force-push noise"}], distinct_size=0)
    assert _summarize_event(e) is None


def test_pr_closed_and_merged_reads_as_merged():
    e = {
        "type": "PullRequestEvent",
        "repo": {"name": "o/r"},
        "payload": {
            "action": "closed",
            "pull_request": {"number": 7, "title": "Add thing", "merged": True,
                             "html_url": "https://github.com/o/r/pull/7"},
        },
        "created_at": "2026-06-01T12:00:00Z",
    }
    out = _summarize_event(e)
    assert out["detail"] == "merged PR #7 Add thing"
    assert out["cls"] == "pr-merged"
    assert out["url"] == "https://github.com/o/r/pull/7"


def test_review_approved_event():
    e = {
        "type": "PullRequestReviewEvent",
        "repo": {"name": "o/r"},
        "payload": {
            "review": {"state": "APPROVED", "html_url": "review-url"},
            "pull_request": {"number": 9, "title": "T"},
        },
        "created_at": "",
    }
    out = _summarize_event(e)
    assert out["detail"] == "approved PR #9 T"
    assert out["cls"] == "review-approved"
    assert out["url"] == "review-url"


def test_issue_comment_truncates_long_first_line():
    e = {
        "type": "IssueCommentEvent",
        "repo": {"name": "o/r"},
        "payload": {
            "issue": {"number": 1, "title": "T"},
            "comment": {"body": "x" * 100 + "\nsecond line", "html_url": "u"},
        },
        "created_at": "",
    }
    out = _summarize_event(e)
    assert ("x" * 77 + "…") in out["detail"]
    assert "second line" not in out["detail"]


def test_create_branch_event_links_tree():
    e = {
        "type": "CreateEvent",
        "repo": {"name": "o/r"},
        "payload": {"ref_type": "branch", "ref": "feat"},
        "created_at": "",
    }
    out = _summarize_event(e)
    assert out["detail"] == "created branch feat"
    assert out["url"] == "https://github.com/o/r/tree/feat"


def test_watch_event_is_star():
    out = _summarize_event({"type": "WatchEvent", "repo": {"name": "o/r"}, "payload": {}, "created_at": ""})
    assert out["kind"] == "star"
    assert out["detail"] == "starred"


def test_unknown_event_type_falls_back_to_generic_row():
    out = _summarize_event({"type": "GollumEvent", "repo": {"name": "o/r"}, "payload": {}, "created_at": ""})
    assert out["kind"] == "gollum"
    assert out["cls"] == "default"


def test_empty_event_returns_none():
    assert _summarize_event({}) is None
