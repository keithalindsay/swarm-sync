"""Chaos-QA regression: POST /lease for an UNKNOWN parcel id (without
`ensure_parcel`) must not leak a raw sqlite IntegrityError as an HTTP 500.

Finding (chaos-faults/chaos-inputs): the lease store deliberately raises
`sqlite3.IntegrityError` when asked to acquire an id with no `parcels` row and
`ensure_parcel=False` (this store-level behavior is intended and covered by
tests/test_leases.py + tests/test_blackboard.py). But the `POST /lease` HTTP
endpoint (`server.app.post_lease`) does not translate that exception, so a
well-formed request that merely names a parcel that does not exist surfaces to
the caller as a bare `500 Internal Server Error` with a leaked stack trace.

That violates DESIGN.md §4.2's explicit error-shape convention, which enumerates
the only wire shapes a caller should ever branch on:

  * 404            -- the named entity does not exist (e.g. /parcel/update on an
                      unknown parcel already returns this),
  * 200 + refusal  -- a policy "no" for a well-formed, understood request,
  * 422            -- a malformed body/param,
  * 401/403/413    -- access control / resource protection.

A 500 is none of these; §4.2 calls the design goal "loud, before any state is
touched, so a caller never mistakes [an error] for an answered question." A
parcel id can legitimately vanish between plan time and lease time -- WP3.5
retires `parcels` rows when a landed merge deletes a file (`parcel_retired`) --
so an in-flight agent re-leasing that id without `ensure_parcel` hits exactly
this path.

The test asserts the OBSERVABLE contract (not the fix site): a well-formed lease
request for a nonexistent parcel gets a clean, non-5xx, structured answer -- a
404, or a 200 refusal body with `granted == false` -- never a leaked server
error. `ensure_parcel=True` on the same id is exercised as the control: it must
still cleanly succeed.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarmsync.server.app import create_app


@pytest.fixture()
def wire_client(tmp_path):
    """A TestClient that RETURNS 5xx responses instead of re-raising the handler
    exception, so the test observes exactly what a real HTTP client (the hook
    adapter, the agent client, httpx) sees on the wire."""
    app = create_app(tmp_path / "blackboard.db")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_lease_unknown_parcel_without_ensure_is_not_a_500(wire_client):
    r = wire_client.post(
        "/lease",
        json={
            "agent_id": "agent-a",
            "parcel_id": "ghost.py::<module>",  # never indexed, no parcels row
            "mode": "write",
        },
    )

    # The core defect: a well-formed request naming a nonexistent parcel must not
    # be answered with a server error (leaked sqlite3.IntegrityError today).
    assert r.status_code < 500, (
        f"POST /lease on an unknown parcel returned {r.status_code}; DESIGN §4.2 "
        f"allows 404 / 200-refusal / 422, never a 5xx. Body: {r.text[:300]!r}"
    )

    # And the answer must be a clean, structured coordination outcome:
    #   * 404  -> "no such parcel" (mirrors /parcel/update), or
    #   * 200  -> a LeaseResult refusal with granted == false.
    if r.status_code == 200:
        body = r.json()
        assert body.get("granted") is False, (
            f"a lease on a nonexistent parcel must not be granted: {body!r}"
        )
    else:
        assert r.status_code == 404, (
            f"expected 404 or a 200 refusal, got {r.status_code}: {r.text[:300]!r}"
        )


def test_lease_unknown_parcel_with_ensure_parcel_still_succeeds(wire_client):
    """Control: the auto-create path is unaffected -- an unknown id with
    ensure_parcel=True still cleanly grants a whole-file lease."""
    r = wire_client.post(
        "/lease",
        json={
            "agent_id": "agent-a",
            "parcel_id": "ghost.py::<module>",
            "mode": "write",
            "ensure_parcel": True,
        },
    )
    assert r.status_code == 200, r.text[:300]
    assert r.json().get("granted") is True
