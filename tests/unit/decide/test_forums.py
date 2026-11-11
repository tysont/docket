"""Which forum an item belongs to (TD §10.2): where it would be raised if the operator chose
to raise it in a meeting and not to do it or track it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from docket.config.models import Profile
from docket.decide.options import forum_for

pytestmark = pytest.mark.smoke
PROFILE = Profile.model_validate({
    "me": {"name": "Operator"},
    "people": [{"id": "sam", "name": "Sam Lee"}, {"id": "kim", "name": "Kim Osei"}, {"id": "ann", "name": "Ann Roe"}],
    "groups": [{"id": "platform", "members": ["kim"], "forum": "platform-sync"},
               {"id": "data", "members": [], "forum": "data-sync"}],
    "topics": [{"id": "queues", "forum": "data-sync"}, {"id": "search"}],
    "forums": [{"id": "one-sam", "kind": "person", "person": "sam"}, {"id": "one-kim", "kind": "person", "person": "kim"},
               {"id": "platform-sync"}, {"id": "data-sync"}],
})
RC = SimpleNamespace(ws=SimpleNamespace(config=SimpleNamespace(profile=PROFILE)))


def item(sender=None, owner=None, topic=None, groups=(), topics=()):
    return {"sender": {"person": sender}, "mentioned": {"groups": list(groups), "topics": list(topics)},
            "assessment": {"owner": owner, "topic": topic} if owner or topic else None}


def fid(it):
    f = forum_for(RC, it)
    return f["id"] if f else None


def test_an_item_is_raised_with_the_person_who_owns_it_before_the_person_who_sent_it():
    """A message from Kim about work that is Sam's belongs in the meeting with Sam."""
    assert fid(item(sender="kim", owner="sam")) == "one-sam"
    assert fid(item(sender="kim", owner="me")) == "one-kim"            # the operator's own: with who raised it
    assert fid(item(sender="kim")) == "one-kim"


def test_without_a_meeting_with_the_person_the_item_goes_to_a_group_or_topic_forum():
    assert fid(item(sender="ann", owner="Somebody Else")) is None      # no forum with either
    assert fid(item(sender="ann", groups=["data"])) == "data-sync"
    assert fid(item(sender="ann", topic="queues")) == "data-sync"      # what it is about has a forum
    assert fid(item(sender="ann", topics=["search", "queues"])) == "data-sync"
    assert fid(item(sender="ann", topic="search")) is None


def test_a_person_forum_wins_over_a_group_forum():
    """Kim is in the platform group, which has a sync, and also has a one-on-one."""
    assert fid(item(sender="kim", groups=["data"])) == "one-kim"
