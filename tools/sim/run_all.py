"""카드 전체(또는 일부)를 시드 N 번 돌리고 채점한다.

python3 tools/sim/run_all.py --seeds 1                 # 전체 34장
python3 tools/sim/run_all.py --seeds 2 --cards u01,u05,a03,b01
python3 tools/sim/run_all.py --cards user --workers 3  # 묶음 이름(user/attacker/benign)도 된다

결과: data/eval/sim_results/<run_id>/<card>_s<seed>.json, oracle.json, judged.json, summary.json, summary.md
API 주소는 환경변수 SIM_API(기본 http://127.0.0.1:18080).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge  # noqa: E402
import simulate  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CARDS = ROOT / "data" / "eval" / "cards"
RESULTS = ROOT / "data" / "eval" / "sim_results"


def load_cards(subset: str | None) -> list[dict]:
    cards = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CARDS.glob("*/*.json"))]
    if not subset:
        return cards
    want = {s.strip() for s in subset.split(",") if s.strip()}
    return [c for c in cards if c["id"] in want or c["kind"] in want]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--cards", default=None, help="쉼표 구분 카드 id 또는 user/attacker/benign")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--run-id", default=None)
    a = ap.parse_args()

    cards = load_cards(a.cards)
    run_id = a.run_id or time.strftime("%Y%m%d-%H%M%S")
    out = RESULTS / run_id
    out.mkdir(parents=True, exist_ok=True)
    api = simulate.Api()

    # 깊이 1 기준선: 정답 유형·상황을 그대로 diagnose 에 넣은 결과(GLM 없음)
    scen_ids = {c["scenario_id"] for c in cards if c["kind"] == "user"}
    if scen_ids:
        scen = {s["id"]: s for s in json.loads((ROOT / "data/eval/scenarios.json").read_text(encoding="utf-8"))}
        oracle = {}
        for sid in sorted(scen_ids):
            st, d, _ = api.call("POST", "/api/diagnose", {"victim_type": scen[sid]["victim_type"], "flags": scen[sid]["flags"]})
            oracle[sid] = d if st == 200 else None
        (out / "oracle.json").write_text(json.dumps(oracle, ensure_ascii=False, indent=1), encoding="utf-8")

    jobs = [(c, s) for s in range(1, a.seeds + 1) for c in cards]
    t0 = time.time()
    print(f"run {run_id}: {len(cards)} cards x {a.seeds} seeds = {len(jobs)} jobs, workers={a.workers}", flush=True)
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(simulate.run_card, c, api, temperature=a.temperature, seed=s): (c, s) for c, s in jobs}
        for i, f in enumerate(as_completed(futs), 1):
            c, s = futs[f]
            res = f.result()
            res["seed"] = s
            (out / f"{c['id']}_s{s}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{i}/{len(jobs)}] {c['id']} s{s} turns={res.get('turns')} wall={res.get('wall_s')}s"
                  f"{' ERROR ' + res['error'] if res.get('error') else ''}", flush=True)
    summary = judge.judge_run(out)
    summary["wall_s"] = round(time.time() - t0, 1)
    summary["seeds"] = a.seeds
    summary["temperature"] = a.temperature
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
