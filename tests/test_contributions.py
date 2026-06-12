"""_from_contribution_collection — GraphQL contributions → event rows."""
from daemon import _from_contribution_collection


def _payload(cc: dict) -> dict:
    return {"data": {"user": {"contributionsCollection": cc}}}


def test_empty_payloads():
    assert _from_contribution_collection({}) == []
    assert _from_contribution_collection(_payload({})) == []


def test_pr_contribution_state_classes_and_draft_tag():
    cc = {"pullRequestContributions": {"nodes": [
        {"occurredAt": "t1", "pullRequest": {"number": 1, "title": "A", "merged": True, "state": "MERGED",
                                             "url": "u1", "repository": {"nameWithOwner": "o/r"}}},
        {"occurredAt": "t2", "pullRequest": {"number": 2, "title": "B", "merged": False, "state": "CLOSED",
                                             "url": "u2", "repository": {"nameWithOwner": "o/r"}}},
        {"occurredAt": "t3", "pullRequest": {"number": 3, "title": "C", "merged": False, "state": "OPEN",
                                             "isDraft": True, "url": "u3",
                                             "repository": {"nameWithOwner": "o/r"}}},
    ]}}
    out = _from_contribution_collection(_payload(cc))
    assert [e["cls"] for e in out] == ["pr-merged", "pr-closed", "pr-open"]
    assert out[2]["detail"].endswith("[draft]")
    assert all(e["kind"] == "pr" and e["repo"] == "o/r" for e in out)


def test_review_verb_mapping_and_url_preference():
    cc = {"pullRequestReviewContributions": {"nodes": [
        {"occurredAt": "t", "pullRequestReview": {"state": "CHANGES_REQUESTED", "url": "review-url"},
         "pullRequest": {"number": 5, "title": "T", "url": "pr-url", "repository": {"nameWithOwner": "o/r"}}},
        {"occurredAt": "t", "pullRequestReview": {"state": "APPROVED"},
         "pullRequest": {"number": 6, "title": "U", "url": "pr-url-6", "repository": {"nameWithOwner": "o/r"}}},
    ]}}
    out = _from_contribution_collection(_payload(cc))
    assert out[0]["detail"] == "requested changes on PR #5 T"
    assert out[0]["cls"] == "review-changes"
    assert out[0]["url"] == "review-url"
    # review without its own url falls back to the PR url
    assert out[1]["cls"] == "review-approved"
    assert out[1]["url"] == "pr-url-6"


def test_issue_contribution_open_vs_closed():
    cc = {"issueContributions": {"nodes": [
        {"occurredAt": "t", "issue": {"number": 8, "title": "I", "state": "OPEN", "url": "u",
                                      "repository": {"nameWithOwner": "o/r"}}},
        {"occurredAt": "t", "issue": {"number": 9, "title": "J", "state": "CLOSED", "url": "u",
                                      "repository": {"nameWithOwner": "o/r"}}},
    ]}}
    out = _from_contribution_collection(_payload(cc))
    assert [e["cls"] for e in out] == ["issue-open", "issue-closed"]


def test_commit_buckets_skip_zero_and_pluralize():
    cc = {"commitContributionsByRepository": [
        {"repository": {"nameWithOwner": "o/r", "url": "https://github.com/o/r"},
         "contributions": {"nodes": [
             {"commitCount": 0, "occurredAt": "t0"},
             {"commitCount": 1, "occurredAt": "t1"},
             {"commitCount": 3, "occurredAt": "t2"},
         ]}},
    ]}
    out = _from_contribution_collection(_payload(cc))
    assert [e["detail"] for e in out] == ["1 commit", "3 commits"]
    assert all(e["kind"] == "push" and e["url"] == "https://github.com/o/r" for e in out)
