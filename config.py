"""
config.py — small shared config surface.

Currently holds EXTRA_TERMS, the operator-configurable addition to
guard.py's hardcoded blocklist (client names, other family words, anything
discovered in the wild that needs blocking but isn't in the SPEC's
hardcoded floor). guard.py imports EXTRA_TERMS defensively (a missing or
broken value here degrades to "no extra terms", never to "no guard").

Add terms here as plain lowercase strings. Word-boundary matching in
guard.py means "acme" won't false-positive on "academic" but WILL catch
"Acme", "ACME", "acme's", etc.

To add terms without editing this file (e.g. from CI or a secret), set the
EXTRA_TERMS_CSV environment variable to a comma-separated list; it's merged
in below.
"""

import os

EXTRA_TERMS = [
    # Example (commented out — uncomment and edit to add a real term):
    # "client-project-codename",
]

_csv = os.environ.get("EXTRA_TERMS_CSV", "").strip()
if _csv:
    EXTRA_TERMS = EXTRA_TERMS + [t.strip() for t in _csv.split(",") if t.strip()]

# Dedup while preserving order.
_seen = set()
_deduped = []
for _t in EXTRA_TERMS:
    _key = _t.lower()
    if _key not in _seen:
        _seen.add(_key)
        _deduped.append(_t)
EXTRA_TERMS = _deduped
