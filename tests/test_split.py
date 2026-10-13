"""Split counterfactual: no split = actual cost; a split drops the old context; no double-charged opening."""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cc_retro import split

W, R = 8.0 / 1e6, 0.2 / 1e6  # opus-5-5 1h write, read per token
T = datetime(2026, 10, 1, tzinfo=timezone.utc)


def c(minute, ctx, writes=0):
    return split.Call(T + timedelta(minutes=minute), ctx, (ctx - writes) * R + writes * W, 0.01, W, R)


class Split(unittest.TestCase):
    def session(self):
        s = split.Session(Path("x.jsonl"), "p")
        s.calls = [c(0, 10_000, 10_000), c(1, 200_000, 1_000), c(2, 201_000, 1_000),
                   c(200, 202_000, 202_000),  # 3h later: a full rebuild
                   c(201, 203_000, 1_000), c(202, 50_000, 50_000)]  # then a compaction
        s.prompts = [split.Prompt(1, T, "go", ""), split.Prompt(3, T + timedelta(minutes=199), "back again", "")]
        return s

    def test_no_split_is_actual(self):
        s = self.session()
        self.assertAlmostEqual(split.counterfactual(s, set()), s.cost)

    def test_idle_split_saves_and_rebuild_pays_no_extra_opening(self):
        s = self.session()
        self.assertEqual(split.idle_boundaries(s, 1.0), {3})
        cf = split.counterfactual(s, {3}, handoff=2000)
        # call 3: ctx 202k -> 10k+2k+(202k-201k)=13k, still all writes; call 4: 203k -> 14k; call 5 compacted: unchanged
        expect = (s.calls[0].in_cost + s.calls[1].in_cost + s.calls[2].in_cost
                  + s.calls[3].in_cost * 13_000 / 202_000 + s.calls[4].in_cost * 14_000 / 203_000
                  + s.calls[5].in_cost + sum(x.out_cost for x in s.calls))
        self.assertAlmostEqual(cf, expect)

    def test_split_mid_flow_pays_the_opening_write(self):
        s = self.session()
        cf = split.counterfactual(s, {2}, handoff=2000)  # call 2 was a cache read; a new session must write
        opening = (10_000 + 2000) * (W - R)
        scaled = s.calls[2].in_cost * 13_000 / 201_000
        self.assertAlmostEqual(cf - (s.cost - s.calls[2].in_cost - s.calls[3].in_cost - s.calls[4].in_cost),
                               opening + scaled + s.calls[3].in_cost * 14_000 / 202_000 + s.calls[4].in_cost * 15_000 / 203_000)


if __name__ == "__main__":
    unittest.main()
