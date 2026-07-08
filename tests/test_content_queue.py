"""
tests/test_content_queue.py — proving content_queue.py's read/write/dedup logic.

Every test uses a temp file path (pytest's tmp_path fixture) so tests never
touch the real content-queue.jsonl in the project root.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import content_queue  # noqa: E402


def test_append_entry_writes_valid_jsonl_line(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    entry = content_queue.append_entry("shipped the new guard module", type="ship", path=path)

    assert entry["raw"] == "shipped the new guard module"
    assert entry["type"] == "ship"
    assert entry["used"] is False
    assert "ts" in entry

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed == entry


def test_append_entry_rejects_invalid_type(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    try:
        content_queue.append_entry("something happened", type="not_a_real_type", path=path)
        assert False, "expected ValueError for invalid type"
    except ValueError:
        pass


def test_append_entry_rejects_empty_raw(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    for bad_raw in ["", "   ", None]:
        try:
            content_queue.append_entry(bad_raw, type="moment", path=path)
            assert False, f"expected ValueError for raw={bad_raw!r}"
        except (ValueError, TypeError):
            pass


def test_append_entry_optional_fields(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    entry = content_queue.append_entry(
        "fixed a race condition",
        type="fix",
        angle="the boring discipline angle",
        shot="/screenshots/fix1.png",
        path=path,
    )
    assert entry["angle"] == "the boring discipline angle"
    assert entry["shot"] == "/screenshots/fix1.png"


def test_get_next_unused_returns_oldest_unused_in_file_order(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    content_queue.append_entry("first entry", type="ship", ts="2026-07-01T00:00:00Z", path=path)
    content_queue.append_entry("second entry", type="fix", ts="2026-07-02T00:00:00Z", path=path)
    content_queue.append_entry("third entry", type="moment", ts="2026-07-03T00:00:00Z", path=path)

    next_entry = content_queue.get_next_unused(path=path)
    assert next_entry["raw"] == "first entry"


def test_get_next_unused_skips_used_entries(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    content_queue.append_entry("first entry", type="ship", ts="2026-07-01T00:00:00Z", path=path)
    content_queue.append_entry("second entry", type="fix", ts="2026-07-02T00:00:00Z", path=path)

    marked = content_queue.mark_used("2026-07-01T00:00:00Z", path=path)
    assert marked is True

    next_entry = content_queue.get_next_unused(path=path)
    assert next_entry["raw"] == "second entry"


def test_get_next_unused_returns_none_when_all_used(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    content_queue.append_entry("only entry", type="ship", ts="2026-07-01T00:00:00Z", path=path)
    content_queue.mark_used("2026-07-01T00:00:00Z", path=path)

    assert content_queue.get_next_unused(path=path) is None


def test_get_next_unused_returns_none_on_missing_file(tmp_path):
    path = str(tmp_path / "does_not_exist.jsonl")
    assert content_queue.get_next_unused(path=path) is None


def test_mark_used_returns_false_for_unknown_ts(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    content_queue.append_entry("only entry", type="ship", ts="2026-07-01T00:00:00Z", path=path)

    result = content_queue.mark_used("2099-01-01T00:00:00Z", path=path)
    assert result is False


def test_mark_used_persists_across_reads(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    content_queue.append_entry("entry one", type="ship", ts="2026-07-01T00:00:00Z", path=path)
    content_queue.mark_used("2026-07-01T00:00:00Z", path=path)

    # Re-read from disk (fresh call) to make sure the mark persisted, not
    # just an in-memory mutation.
    all_unused = content_queue.get_all_unused(path=path)
    assert all_unused == []


def test_get_all_unused_returns_all_in_order(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    content_queue.append_entry("a", type="ship", ts="2026-07-01T00:00:00Z", path=path)
    content_queue.append_entry("b", type="fix", ts="2026-07-02T00:00:00Z", path=path)
    content_queue.append_entry("c", type="moment", ts="2026-07-03T00:00:00Z", path=path)
    content_queue.mark_used("2026-07-02T00:00:00Z", path=path)

    unused = content_queue.get_all_unused(path=path)
    assert [e["raw"] for e in unused] == ["a", "c"]


def test_count_unused(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    content_queue.append_entry("a", type="ship", ts="2026-07-01T00:00:00Z", path=path)
    content_queue.append_entry("b", type="fix", ts="2026-07-02T00:00:00Z", path=path)
    assert content_queue.count_unused(path=path) == 2
    content_queue.mark_used("2026-07-01T00:00:00Z", path=path)
    assert content_queue.count_unused(path=path) == 1


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    path = str(tmp_path / "queue.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"ts": "2026-07-01T00:00:00Z", "type": "ship", "raw": "good entry", "used": false}\n')
        f.write("THIS IS NOT VALID JSON\n")
        f.write('{"ts": "2026-07-02T00:00:00Z", "type": "fix", "raw": "another good entry", "used": false}\n')

    unused = content_queue.get_all_unused(path=path)
    assert len(unused) == 2
    assert unused[0]["raw"] == "good entry"
    assert unused[1]["raw"] == "another good entry"


def test_used_field_missing_is_treated_as_unused(tmp_path):
    """An entry written by hand without a `used` key at all should still surface as unused."""
    path = str(tmp_path / "queue.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"ts": "2026-07-01T00:00:00Z", "type": "ship", "raw": "no used field"}\n')

    entry = content_queue.get_next_unused(path=path)
    assert entry is not None
    assert entry["raw"] == "no used field"


def test_appended_entries_are_valid_types_only():
    assert content_queue.VALID_TYPES == {"ship", "fix", "decision", "moment", "receipt"}


# --- SPEC-v3 pillar tagging + rotation --------------------------------------

def test_append_entry_accepts_valid_pillar(tmp_path):
    path = str(tmp_path / "q.jsonl")
    entry = content_queue.append_entry("a showcase", type="ship", pillar="SHOWCASE", path=path)
    assert entry["pillar"] == "showcase"  # normalized lowercase


def test_append_entry_rejects_invalid_pillar(tmp_path):
    path = str(tmp_path / "q.jsonl")
    try:
        content_queue.append_entry("x", type="ship", pillar="not-a-pillar", path=path)
        assert False, "expected ValueError for invalid pillar"
    except ValueError:
        pass


def _seed(path, raw, pillar, ts):
    content_queue.append_entry(raw, type="moment", pillar=pillar, ts=ts, path=path)


def test_rotation_no_history_returns_first_unused(tmp_path):
    path = str(tmp_path / "q.jsonl")
    _seed(path, "a", "showcase", "2026-07-01T00:00:00Z")
    _seed(path, "b", "operator", "2026-07-02T00:00:00Z")
    assert content_queue.get_next_rotated([], path=path)["raw"] == "a"


def test_rotation_skips_same_pillar_as_last(tmp_path):
    path = str(tmp_path / "q.jsonl")
    _seed(path, "a", "showcase", "2026-07-01T00:00:00Z")
    _seed(path, "b", "operator", "2026-07-02T00:00:00Z")
    # Last posted pillar was showcase -> skip "a", pick "b".
    assert content_queue.get_next_rotated(["showcase"], path=path)["raw"] == "b"


def test_rotation_meta_capped_within_window(tmp_path):
    path = str(tmp_path / "q.jsonl")
    _seed(path, "m", "meta", "2026-07-01T00:00:00Z")
    _seed(path, "s", "showcase", "2026-07-02T00:00:00Z")
    # META already appeared 1 post ago (inside the 5-window) -> meta is blocked,
    # so "m" is skipped and the substance post "s" wins.
    picked = content_queue.get_next_rotated(["meta"], path=path)
    assert picked["raw"] == "s"


def test_rotation_meta_allowed_once_window_clears(tmp_path):
    path = str(tmp_path / "q.jsonl")
    _seed(path, "m", "meta", "2026-07-01T00:00:00Z")
    # META last appeared 5+ posts ago (outside the window) -> allowed again.
    recent = ["showcase", "operator", "question", "operator"]  # len 4, no meta
    assert content_queue.get_next_rotated(recent, path=path)["raw"] == "m"


def test_rotation_relaxes_consecutive_but_never_meta_cap(tmp_path):
    path = str(tmp_path / "q.jsonl")
    # Only same-pillar substance left -> no-consecutive is relaxed (post it).
    _seed(path, "only", "showcase", "2026-07-01T00:00:00Z")
    assert content_queue.get_next_rotated(["showcase"], path=path)["raw"] == "only"


def test_rotation_returns_none_when_only_meta_and_meta_blocked(tmp_path):
    path = str(tmp_path / "q.jsonl")
    _seed(path, "m", "meta", "2026-07-01T00:00:00Z")
    # Only a META entry remains AND meta is inside the cap window -> nothing
    # postable (never relax the META cap). Skip the tick rather than burst meta.
    assert content_queue.get_next_rotated(["meta"], path=path) is None


def test_rotation_untagged_entry_never_blocks(tmp_path):
    path = str(tmp_path / "q.jsonl")
    # Legacy entry with no pillar field at all.
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-07-01T00:00:00Z", "type": "ship", "raw": "legacy", "used": False}) + "\n")
    # Even if the last pillar was "" it should still post (untagged != consecutive block).
    assert content_queue.get_next_rotated(["showcase"], path=path)["raw"] == "legacy"
