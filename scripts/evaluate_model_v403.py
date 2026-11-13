#!/usr/bin/env python3
"""Run the 100 additional Chinese samples against the configured real model.

This measures *model-path* behaviour only: each utterance is chosen so the local
rules cannot answer it, forcing a semantic call. Reported numbers are real-model
results for this corpus, not ASR accuracy and not end-to-end success.

Per-intent accuracy, schema validity and stage latencies (route/LLM) are
recorded; execution and feedback stages are measured elsewhere (real-device
runs) because this script never touches a TV.
"""
from __future__ import annotations
import argparse
import copy
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def percentile(values, p):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * p) - 1)], 1) if values else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--context", choices=("cold", "played"), default="played",
                        help="played seeds a real prior item so references are resolvable")
    args = parser.parse_args()
    from adbtv.config import load_config
    from adbtv.voice_intelligence import IntelligentVoiceRouter

    cases = json.loads((ROOT / "tests/fixtures/voice_zh_v403_model.json").read_text())
    from adbtv.conversation import ConversationManager
    context = {}
    if args.context == "played":
        manager = ConversationManager()
        manager.set_results("eval", [{"id": "seed1", "title": "三体", "genres": ["科幻"],
                                      "region": "中国大陆", "season": 1, "episode": 3}])
        manager.select("eval", index=1)
        manager.remember_play("eval", {"id": "seed1", "title": "三体", "genres": ["科幻"],
                                       "region": "中国大陆", "season": 1, "episode": 3})
        manager.update_observation("eval", app="com.trim.tv",
                                   playback={"title": "三体", "season": 1,
                                             "episode": 3, "state": "paused"})
        context = manager.snapshot("eval")
    cfg = copy.deepcopy(load_config())
    cfg.setdefault("voice", {}).setdefault("intelligence", {})["enabled"] = True
    router = IntelligentVoiceRouter(cfg)
    rows = []
    for index, case in enumerate(cases[:args.limit]):
        if index and args.interval:
            time.sleep(min(args.interval, 5))
        started = time.monotonic()
        decision = router.decide(case["text"], context)
        elapsed = (time.monotonic() - started) * 1000
        expected = case["expected_intent"]
        # A clarification is a correct semantic outcome for ambiguous requests.
        # A reference can be satisfied by selecting or resuming the same item.
        play_like = {"play", "select", "resume_episode"}
        matched = (decision.intent == expected
                   or (expected == "play" and decision.intent in play_like)
                   or (expected == "clarify" and decision.route == "clarify"))
        row = {"text_index": index, "expected": expected, "matched": bool(matched),
               "route": decision.route, "intent": decision.intent,
               "source": decision.source, "reason": decision.reason,
               "confidence": decision.confidence, "risk": decision.risk,
               "llm_ms": round(decision.llm_ms, 1), "route_ms": round(elapsed, 1)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    by_intent = defaultdict(lambda: {"n": 0, "ok": 0})
    for row in rows:
        bucket = by_intent[row["expected"]]
        bucket["n"] += 1
        bucket["ok"] += int(row["matched"])
    accuracy = {k: round(v["ok"] * 100 / v["n"], 1) for k, v in by_intent.items()}
    llm = [r["llm_ms"] for r in rows if r["source"] == "llm"]
    summary = {
        "corpus": "tests/fixtures/voice_zh_v403_model.json",
        "cases": len(rows), "real_model_requests": len(llm),
        "overall_match_percent": round(sum(r["matched"] for r in rows) * 100 / len(rows), 1),
        "by_intent": {k: dict(v, accuracy_percent=accuracy[k]) for k, v in by_intent.items()},
        "schema_valid_percent": round(len(llm) * 100 / len(rows), 1),
        "llm_latency_ms": {"p50": percentile(llm, .5), "p95": percentile(llm, .95)},
        "route_latency_ms": {"p50": percentile([r["route_ms"] for r in rows], .5),
                             "p95": percentile([r["route_ms"] for r in rows], .95)},
        "asr_accuracy": None, "execution_success": None,
        "context": args.context,
        "note": "model-path only; execution/feedback measured on the real TV separately",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"summary": summary, "rows": rows},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()