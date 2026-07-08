"""
api/slack_events.py — Vercel serverless function: Slack Events → GitHub dispatch (SPEC-v4).

Thin HTTP adapter. All security-relevant logic lives in slack_trigger.py (unit-
tested). This file only: reads the request, calls verify_signature (fail-closed),
calls decide(), and — on a valid operator 👍 — fires ONE repository_dispatch. It
performs NO action logic (I-TRIGGER-NOT-ACTION); the richest thing it does is wake
the fully-gated scout-act workflow.

Runtime: Vercel Python (zero third-party deps — stdlib hmac/urllib only). The
Request URL is https://<deployment>/api/slack_events.

Host env (secrets, never in repo — see OPERATOR-SETUP-event-trigger.md):
  SLACK_SIGNING_SECRET, GITHUB_DISPATCH_TOKEN, GITHUB_REPO ("owner/repo"),
  SLACK_OPERATOR_USER_ID, SLACK_CHANNEL_ID.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

# Make the root-level slack_trigger module importable both on Vercel and locally.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slack_trigger  # noqa: E402


def _github_dispatch(repo: str, token: str, payload: dict, timeout: int = 8) -> int:
    """
    POST /repos/{repo}/dispatches. Returns the HTTP status (204 on success).
    Never raises — a failure returns a status code (or 0) so the caller can log
    and still ack Slack (the cron fallback keeps acting; the operator fixes the PAT).
    """
    url = f"https://api.github.com/repos/{repo}/dispatches"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "vibekoded-slack-trigger")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        print(f"[slack_events] github dispatch HTTP {exc.code}: {exc.read()[:200]!r}")
        return exc.code
    except Exception as exc:  # noqa: BLE001
        print(f"[slack_events] github dispatch error: {exc!r}")
        return 0


class handler(BaseHTTPRequestHandler):  # Vercel Python entrypoint (class named `handler`)
    def _send(self, code: int, body: str = "", content_type: str = "text/plain") -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        # I-SIG-VERIFY (fail-closed): authenticate BEFORE parsing/acting.
        signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
        ts = self.headers.get("X-Slack-Request-Timestamp")
        sig = self.headers.get("X-Slack-Signature")
        if not slack_trigger.verify_signature(signing_secret, ts, raw, sig):
            self._send(401, "invalid signature")
            return

        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send(400, "bad json")
            return

        decision = slack_trigger.decide(
            body,
            operator_id=os.environ.get("SLACK_OPERATOR_USER_ID", ""),
            target_channel=os.environ.get("SLACK_CHANNEL_ID", ""),
        )
        action = decision.get("action")

        if action == "challenge":
            # Slack setup handshake: echo the challenge value.
            self._send(200, decision.get("challenge", ""))
            return

        if action == "dispatch":
            repo = os.environ.get("GITHUB_REPO", "")
            token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
            if repo and token:
                status = _github_dispatch(repo, token, slack_trigger.build_dispatch_payload(body))
                print(f"[slack_events] dispatched slack-thumbsup -> github status {status}")
            else:
                print("[slack_events] GITHUB_REPO / GITHUB_DISPATCH_TOKEN unset; cannot dispatch.")
            # Always ack fast regardless of dispatch result (I-FAST-ACK) — a
            # retrying Slack won't fix a bad PAT, and duplicates are idempotent.
            self._send(200, "ok")
            return

        # ignore: valid request, nothing to do (non-operator / wrong emoji / etc.)
        print(f"[slack_events] ignored: {decision.get('reason')}")
        self._send(200, "ignored")
