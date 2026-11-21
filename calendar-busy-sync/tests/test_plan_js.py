"""The pack's deterministic `plan` step, run under node exactly as written in
the manifest: which busy blocks get created, moved and removed.

    python3 -m unittest discover -s calendar-busy-sync/tests

Needs node and PyYAML.
"""
import json
import os
import subprocess
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "..", "conductor-pack.yaml")
TAG = "conductorBusySync"

CFG = {
    "window_days": "30",
    "block_title": "Busy",
    "show_details": "false",
    "color_id": "8",
    "mirror_all_day": "false",
    "tag": TAG,
    "max_deletes": "50",
    "batch_size": "50",
    "remove_all": "false",
}


def step_code(step_id):
    with open(MANIFEST) as f:
        man = yaml.safe_load(f)
    for s in man["workflows"]["reconcile"]["steps"]:
        if s.get("id") == step_id:
            return s["code"]
    raise KeyError(step_id)


def run_js(code, ctx):
    """Run a js step body the way the engine does: `ctx` global, body returns."""
    src = "const ctx = JSON.parse(process.argv[1]);\n" \
          "const out = (function () {\n" + code + "\n})();\n" \
          "process.stdout.write(JSON.stringify(out));\n"
    p = subprocess.run(["node", "-e", src, json.dumps(ctx)], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())
    return json.loads(p.stdout)


def ev(eid, start, end, **extra):
    e = {"id": eid, "status": "confirmed", "iCalUID": eid + "@google.com",
         "summary": "Thing " + eid, "start": {"dateTime": start}, "end": {"dateTime": end}}
    e.update(extra)
    return e


def block(eid, key, start, end, **extra):
    e = {"id": eid, "status": "confirmed", "iCalUID": eid + "@google.com", "summary": "Busy",
         "start": {"dateTime": start}, "end": {"dateTime": end},
         "transparency": "opaque", "visibility": "private", "colorId": "8",
         "extendedProperties": {"private": {TAG: key}}}
    e.update(extra)
    return e


def plan(src, dst, **cfg):
    ctx = {
        "cfg": dict(CFG, **cfg),
        "src": {"by_member": {k: {"items": v} for k, v in src.items()}},
        "dst": {"by_member": {k: {"items": v} for k, v in dst.items()}},
    }
    return run_js(step_code("plan"), ctx)


T1, T2 = "2026-12-01T15:00:00Z", "2026-12-01T16:00:00Z"


class PlanTest(unittest.TestCase):
    def test_personal_event_blocks_every_work_calendar(self):
        out = plan({"personal": [ev("p1", T1, T2)], "work": [], "side": []},
                   {"work": [], "side": []})
        self.assertEqual([c["target"] for c in out["creates"]], ["side", "work"])
        b = out["creates"][0]["event"]
        self.assertEqual(b["summary"], "Busy")
        self.assertEqual(b["visibility"], "private")
        self.assertEqual(b["transparency"], "opaque")
        self.assertEqual(b["reminders"], {"useDefault": False, "overrides": []})
        self.assertEqual(b["extendedProperties"]["private"][TAG], "personal/p1")
        self.assertEqual(b["start"], {"dateTime": T1})
        self.assertEqual((out["updates"], out["deletes"]), ([], []))
        self.assertIn("blocks wanted: personal -> side 1, personal -> work 1", out["summary"])

    def test_a_calendar_never_blocks_itself(self):
        out = plan({"work": [ev("w1", T1, T2)], "side": []}, {"work": [], "side": []})
        self.assertEqual([c["target"] for c in out["creates"]], ["side"])

    def test_second_run_is_a_noop_even_across_time_zones(self):
        # The target hands the same instant back in its own offset.
        have = block("b1", "personal/p1", "2026-12-01T08:00:00-07:00", "2026-12-01T09:00:00-07:00")
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [have]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))

    def test_moved_event_moves_its_block(self):
        have = block("b1", "personal/p1", T1, T2)
        out = plan({"personal": [ev("p1", "2026-12-01T17:00:00Z", "2026-12-01T18:00:00Z")]},
                   {"work": [have]})
        self.assertEqual(out["creates"], [])
        self.assertEqual(out["updates"][0]["event_id"], "b1")
        self.assertEqual(out["updates"][0]["event"]["start"], {"dateTime": "2026-12-01T17:00:00Z"})

    def test_gone_or_cancelled_event_removes_its_block(self):
        have = [block("b1", "personal/p1", T1, T2), block("b2", "personal/p2", T1, T2)]
        out = plan({"personal": [ev("p2", T1, T2, status="cancelled")]}, {"work": have})
        self.assertEqual(sorted(d["event_id"] for d in out["deletes"]), ["b1", "b2"])

    def test_events_that_do_not_block_time_are_skipped(self):
        src = [
            ev("free", T1, T2, transparency="transparent"),
            ev("declined", T1, T2, attendees=[{"self": True, "responseStatus": "declined"}]),
            ev("loc", T1, T2, eventType="workingLocation"),
            {"id": "allday", "status": "confirmed", "start": {"date": "2026-12-01"}, "end": {"date": "2026-12-02"}},
        ]
        out = plan({"personal": src}, {"work": []})
        self.assertEqual(out["creates"], [])
        out = plan({"personal": src}, {"work": []}, mirror_all_day="true")
        self.assertEqual([c["event"]["start"] for c in out["creates"]], [{"date": "2026-12-01"}])

    def test_accepted_invite_blocks(self):
        src = [ev("ok", T1, T2, attendees=[{"self": True, "responseStatus": "accepted"}])]
        self.assertEqual(len(plan({"personal": src}, {"work": []})["creates"]), 1)

    def test_a_block_is_never_mirrored_onward(self):
        # work holds a block for a personal event; side must get ONE block (from
        # personal), not a second one copied from work's block.
        work_block = block("b1", "personal/p1", T1, T2)
        out = plan({"personal": [ev("p1", T1, T2)], "work": [work_block], "side": []},
                   {"work": [work_block], "side": []})
        self.assertEqual([(c["target"], c["event"]["extendedProperties"]["private"][TAG])
                          for c in out["creates"]], [("side", "personal/p1")])

    def test_meeting_already_on_the_target_gets_no_block(self):
        # Invited on both accounts: same iCalUID and start on both calendars.
        w = ev("m1", T1, T2)
        s = ev("m1x", "2026-12-01T08:00:00-07:00", "2026-12-01T09:00:00-07:00", iCalUID=w["iCalUID"])
        out = plan({"work": [w], "side": [s]}, {"work": [w], "side": [s]})
        self.assertEqual(out["creates"], [])

    def test_same_meeting_on_two_sources_blocks_a_third_once(self):
        w = ev("m1", T1, T2)
        s = ev("m1x", T1, T2, iCalUID=w["iCalUID"])
        out = plan({"work": [w], "side": [s]}, {"personal": []})
        self.assertEqual(len(out["creates"]), 1)

    def test_duplicate_blocks_are_collapsed(self):
        have = [block("b1", "personal/p1", T1, T2), block("b2", "personal/p1", T1, T2)]
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": have})
        self.assertEqual([d["event_id"] for d in out["deletes"]], ["b2"])
        self.assertEqual((out["creates"], out["updates"]), ([], []))

    def test_show_details_copies_the_title(self):
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": []}, show_details="true")
        self.assertEqual(out["creates"][0]["event"]["summary"], "Thing p1")

    def test_other_events_on_a_target_are_left_alone(self):
        out = plan({"personal": []}, {"work": [ev("w1", T1, T2)]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))

    def test_a_run_is_capped_at_batch_size_and_the_rest_carries_over(self):
        src = [ev("p%d" % i, T1, T2) for i in range(7)]
        out = plan({"personal": src}, {"work": []}, batch_size="3")
        self.assertEqual(len(out["creates"]), 3)
        self.assertEqual(out["pending"], 4)
        self.assertIn("7 to create", out["summary"])
        self.assertIn("batch_size 3, 4 left for later runs", out["summary"])
        # The next run sees the three blocks that now exist and plans the rest.
        have = [block("b%d" % i, c["event"]["extendedProperties"]["private"][TAG], T1, T2)
                for i, c in enumerate(out["creates"])]
        out = plan({"personal": src}, {"work": have}, batch_size="3")
        self.assertEqual((len(out["creates"]), out["pending"]), (3, 1))

    def test_nothing_pending_is_not_mentioned(self):
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": []})
        self.assertEqual(out["pending"], 0)
        self.assertNotIn("left for later", out["summary"])

    def test_time_already_blocked_by_hand_gets_no_block(self):
        # The same commitment entered separately on both calendars.
        p, w = ev("p1", T1, T2), ev("w1", "2026-12-01T08:00:00-07:00", "2026-12-01T09:00:00-07:00")
        out = plan({"personal": [p], "work": [w]}, {"work": [w], "personal": [p]})
        self.assertEqual((out["creates"], out["updates"], out["deletes"]), ([], [], []))
        self.assertIn("2 already blocked on the target", out["summary"])

    def test_a_longer_event_on_the_target_covers_a_shorter_one(self):
        w = ev("w1", "2026-12-01T14:00:00Z", "2026-12-01T17:00:00Z")
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [w]})
        self.assertEqual(out["creates"], [])

    def test_partial_overlap_still_gets_a_block(self):
        w = ev("w1", "2026-12-01T15:30:00Z", "2026-12-01T16:30:00Z")
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [w]})
        self.assertEqual(len(out["creates"]), 1)

    def test_free_or_declined_target_events_do_not_cover(self):
        for extra in ({"transparency": "transparent"},
                      {"attendees": [{"self": True, "responseStatus": "declined"}]}):
            out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [ev("w1", T1, T2, **extra)]})
            self.assertEqual(len(out["creates"]), 1, extra)

    def test_a_block_does_not_count_as_the_targets_own_time(self):
        have = block("b1", "personal/p1", T1, T2)
        out = plan({"personal": [ev("p1", T1, T2)]}, {"work": [have]})
        self.assertEqual((out["creates"], out["deletes"]), ([], []))

    def test_redundant_blocks_are_removed_whatever_their_number(self):
        # Blocks made before the target's own events were taken into account.
        src = [ev("p%d" % i, T1, T2) for i in range(5)]
        have = [block("b%d" % i, "personal/p%d" % i, T1, T2) for i in range(5)]
        out = plan({"personal": src}, {"work": have + [ev("w1", T1, T2)]}, max_deletes="1")
        self.assertEqual(len(out["deletes"]), 5)
        self.assertEqual(out["creates"], [])

    def test_mass_delete_is_refused(self):
        have = [block("b%d" % i, "personal/p%d" % i, T1, T2) for i in range(4)]
        with self.assertRaisesRegex(RuntimeError, "refusing to delete 4 blocks"):
            plan({"personal": []}, {"work": have}, max_deletes="3")

    def test_unbinding_a_source_removes_its_blocks_without_tripping_the_guard(self):
        have = [block("b%d" % i, "side/s%d" % i, T1, T2) for i in range(4)]
        out = plan({"personal": []}, {"work": have}, max_deletes="1")
        self.assertEqual(len(out["deletes"]), 4)

    def test_remove_all_clears_every_block_and_creates_none(self):
        have = [block("b%d" % i, "personal/p%d" % i, T1, T2) for i in range(4)]
        out = plan({"personal": [ev("p9", T1, T2)]}, {"work": have + [ev("w1", T1, T2)]},
                   remove_all="true", max_deletes="1")
        self.assertEqual(sorted(d["event_id"] for d in out["deletes"]), ["b0", "b1", "b2", "b3"])
        self.assertEqual((out["creates"], out["updates"]), ([], []))
        self.assertIn("remove_all is on", out["summary"])


class WindowTest(unittest.TestCase):
    def test_window_spans_the_setting(self):
        out = run_js(step_code("window"), {"cfg": dict(CFG, window_days="7")})
        from datetime import datetime
        lo = datetime.fromisoformat(out["timeMin"].replace("Z", "+00:00"))
        hi = datetime.fromisoformat(out["timeMax"].replace("Z", "+00:00"))
        self.assertEqual((hi - lo).days, 7)

    def test_bad_window_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "window_days"):
            run_js(step_code("window"), {"cfg": dict(CFG, window_days="0")})


if __name__ == "__main__":
    unittest.main()
