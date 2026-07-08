"""
ensure_bot_label.py — one-shot, idempotent setter for the profile `bot` self-label.

The I-BOT-DISCLOSED safety basis (SPEC-v3): marks the account automated via the
Bluesky `bot` self-label on app.bsky.actor.profile. Run it manually or via the
self-label workflow (workflow_dispatch). Safe to re-run — it no-ops when already set.

Exit 0 on success (set / already_set / created), 1 on failure.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import bluesky

        session = bluesky.create_session()
        result = bluesky.ensure_bot_label(session)
    except Exception as exc:  # noqa: BLE001
        print(f"[ensure_bot_label] FAILED: {exc!r}")
        return 1

    status = result.get("status")
    print(f"[ensure_bot_label] bot self-label status: {status} (handle={session.get('handle')})")
    if status == "already_set":
        print("[ensure_bot_label] ✓ already labelled 'bot' — nothing to do.")
    elif status in ("set", "created"):
        print(f"[ensure_bot_label] ✓ 'bot' self-label written (cid={result.get('cid')}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
