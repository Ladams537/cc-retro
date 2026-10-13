"""Score cc-retro's prompt-level detectors (retry, correction) against hand labels.

    PYTHONPATH=src python3 eval/eval_prompts.py [--classify]

Labels live in eval/gold.local.json (gitignored: it's derived from your own prompts).
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from cc_retro import classify  # noqa: E402
from cc_retro.parse import load_all  # noqa: E402


def key(text: str) -> str:
    return hashlib.sha256(" ".join(text.split())[:230].encode()).hexdigest()[:16]


def score(name, pred: set, gold: set, universe: int) -> None:
    tp, fp, fn = len(pred & gold), len(pred - gold), len(gold - pred)
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    print(f"{name:11} precision {tp}/{tp + fp} = {p:.2f}   recall {tp}/{tp + fn} = {r:.2f}   (of {universe} prompts)")


def main() -> None:
    gold = json.loads((Path(__file__).parent / "gold.local.json").read_text())["labels"]
    sessions = load_all()
    if "--classify" in sys.argv:
        classify.classify(sessions)
    prompts = {key(p.text): (s, p) for s in sessions for p in s.prompts if key(p.text) in gold}
    retry_ts = {(f.session_id, f.ts) for s in sessions for f in s.friction if f.kind == "retry"}
    corr_ts = {(f.session_id, f.ts) for s in sessions for f in s.friction if f.kind == "correction"}
    pred_r = {k for k, (s, p) in prompts.items() if (s.id, p.ts) in retry_ts}
    pred_c = {k for k, (s, p) in prompts.items() if (s.id, p.ts) in corr_ts}
    for label, pred in (("retry", pred_r), ("correction", pred_c)):
        g = {k for k in prompts if gold[k] == label}
        score(label, pred, g, len(prompts))
        for k in sorted(pred ^ g):
            tag = "FP" if k in pred else "FN"
            print(f"   {tag}: {' '.join(prompts[k][1].text.split())[:110]}")


if __name__ == "__main__":
    main()
