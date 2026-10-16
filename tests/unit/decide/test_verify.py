"""Task verification readings (TD §10.5): closed states and the operator as the last actor."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from docket.config.models import VerifyConfig
from docket.decide.verify import looks_done

pytestmark = pytest.mark.smoke


def rc(**verify):
    me = SimpleNamespace(name="Dana Reyes", aliases=["Dana"], handles={"chat": "users/1001"})
    cfg = SimpleNamespace(decisions=SimpleNamespace(verify=VerifyConfig(**verify)), profile=SimpleNamespace(me=me))
    return SimpleNamespace(ws=SimpleNamespace(config=cfg))


def res(**kw):
    return {"status": "resolved", "source": "tracker", **kw}


@pytest.mark.parametrize("r,task_date,done", [
    (res(state="Closed"), None, True),
    (res(state="merged"), "2026-10-10", True),
    (res(state="open", last_actor="users/1001", last_activity="2026-10-15T10:00:00Z"), "2026-10-14", True),
    (res(state="open", last_actor="Dana", last_activity="2026-10-15T10:00:00Z"), None, True),
    (res(state="open", last_actor="users/1001", last_activity="2026-10-13T10:00:00Z"), "2026-10-14", False),
    (res(state="open", last_actor="users/3003", last_activity="2026-10-15T10:00:00Z"), None, False),
    ({"status": "failed", "state": "closed"}, None, False),
    ({"status": "unresolvable"}, None, False),
])
def test_looks_done(r, task_date, done):
    assert (looks_done(rc(), r, task_date) is not None) is done


def test_closed_states_are_configurable():
    assert looks_done(rc(closed_states=["shipped"]), res(state="shipped"), None)
    assert looks_done(rc(closed_states=["shipped"]), res(state="closed"), None) is None
