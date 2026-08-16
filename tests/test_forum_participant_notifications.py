"""Regression tests for reply notifications to prior forum participants."""

import os

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

from app import _forum_reply_participant_tokens


def test_prior_participants_are_deduplicated_and_special_recipients_excluded():
    replies = [
        {"id": "r1", "author_token": "thread-author"},
        {"id": "r2", "author_token": "viola"},
        {"id": "r3", "author_token": "viola"},
        {"id": "r4", "author_token": "parent-author"},
        {"id": "new", "author_token": "miguel"},
        {"id": "r5", "author_token": "other"},
    ]

    assert _forum_reply_participant_tokens(
        replies,
        current_reply_id="new",
        excluded_tokens={"miguel", "thread-author", "parent-author"},
    ) == ["viola", "other"]


def test_participant_notifications_are_bounded_and_ignore_empty_tokens():
    replies = [
        {"id": "empty", "author_token": ""},
        {"id": "a", "author_token": " a "},
        {"id": "b", "author_token": "b"},
        {"id": "c", "author_token": "c"},
    ]

    assert _forum_reply_participant_tokens(replies, limit=2) == ["a", "b"]
