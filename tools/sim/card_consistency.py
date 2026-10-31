"""카드 → 정답 일관성 점검(서비스 API 없이 GLM 만).

사용자 카드마다 GLM(사용자 역할)이 빈 체크 항목 양식(유형 라디오 + 상황 체크박스)을 카드 사실대로
채우게 하고, 그 답을 시나리오 정답(유형·flags)과 비교한다. 카드가 정답을 제대로 암시하는지
보는 검사다. 정답 밖 flag 를 반복해서 체크하면 카드가 그 상황을 암시한다는 뜻이다.

python3 tools/sim/card_consistency.py --seeds 2 --out data/eval/sim_results/<run_id>/card_consistency.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cards as cardset  # noqa: E402
import simulate  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    labels = {f["id"]: f["label"] for f in json.loads((ROOT / "data/knowledge.json").read_text(encoding="utf-8"))["flags"]}
    scen = {s["id"]: s for s in json.loads((ROOT / "data/eval/scenarios.json").read_text(encoding="utf-8"))}
    users = [c for c in cardset.load("v2") if c["kind"] == "user"]
    jobs = [(c, s) for c in users for s in range(1, a.seeds + 1)]

    def one(job):
        c, s = job
        vt, fl, ev = simulate.fill_form(c, labels, None, [], a.temperature, s)
        g = scen[c["scenario_id"]]
        return {"card": c["id"], "seed": s, "parsed": ev["parsed"], "victim_type": vt, "gold_type": g["victim_type"],
                "type_ok": vt == g["victim_type"], "flags": fl,
                "missing": sorted(set(g["flags"]) - set(fl)), "extra": sorted(set(fl) - set(g["flags"]))}

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        rows = list(ex.map(one, jobs))
    tp = sum(len(set(r["flags"]) - set(r["extra"])) for r in rows)
    fp = sum(len(r["extra"]) for r in rows)
    fn = sum(len(r["missing"]) for r in rows)
    summ = {
        "n": len(rows), "parsed": sum(r["parsed"] for r in rows),
        "type_acc": round(sum(r["type_ok"] for r in rows) / len(rows), 3),
        "flags_precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "flags_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "exact": sum(1 for r in rows if r["type_ok"] and not r["missing"] and not r["extra"]),
        "extra_counts": dict(Counter(f for r in rows for f in r["extra"]).most_common()),
        "missing_counts": dict(Counter(f for r in rows for f in r["missing"]).most_common()),
        "type_errors": [f"{r['card']}_s{r['seed']}:{r['victim_type']}" for r in rows if not r["type_ok"]],
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summ, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summ, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
