"""
smoke_test.py — OPT-IN live check against the real Bluesky API.

This is NOT part of the pytest suite (tests/ never makes network calls).
Run this by hand once you have real BSKY_HANDLE / BSKY_APP_PASSWORD set,
to confirm credentials work end-to-end BEFORE trusting the automated cron.

Usage:
    python smoke_test.py            # login + notification fetch only (safe, no post)
    python smoke_test.py --post     # ALSO posts one real, clearly-marked test post

The --post variant publishes a real post to the real account. Only run it
when you're ready for that (e.g. right before going live, so the account's
first post is intentional). It always guard-checks the text first, same as
production code paths — belt and suspenders even for a hardcoded string.
"""

from __future__ import annotations

import sys

import bluesky
import guard


def main() -> int:
    do_post = "--post" in sys.argv[1:]

    print("[smoke_test] attempting create_session()...")
    try:
        session = bluesky.create_session()
    except bluesky.BlueskyError as exc:
        print(f"[smoke_test] FAILED create_session(): {exc}")
        print("[smoke_test] check BSKY_HANDLE / BSKY_APP_PASSWORD env vars.")
        return 1

    print(f"[smoke_test] OK — logged in as handle={session.get('handle')} did={session.get('did')}")

    print("[smoke_test] attempting get_notifications()...")
    try:
        notifications = bluesky.get_notifications(session=session, limit=5)
        print(f"[smoke_test] OK — fetched {len(notifications)} notification(s).")
    except bluesky.BlueskyError as exc:
        print(f"[smoke_test] FAILED get_notifications(): {exc}")
        return 1

    if not do_post:
        print("[smoke_test] skipping live post (pass --post to publish a real test post).")
        print("[smoke_test] ALL CHECKS PASSED (read-only).")
        return 0

    test_text = "vibekoded social engine — smoke test post. confirming the pipe works. (this account is automated.)"
    ok, reason = guard.check(test_text)
    if not ok:
        print(f"[smoke_test] guard blocked the smoke-test text itself (should never happen): {reason}")
        return 1

    print(f"[smoke_test] posting live test post:\n{test_text}")
    try:
        result = bluesky.post(test_text, session=session)
    except bluesky.BlueskyError as exc:
        print(f"[smoke_test] FAILED post(): {exc}")
        return 1

    print(f"[smoke_test] OK — posted. uri={result.get('uri')} cid={result.get('cid')}")
    print("[smoke_test] ALL CHECKS PASSED (including live post — go check the account).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
