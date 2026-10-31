"""내부 LLM 평가 (SPEC §8) → data/eval/results_llm.json.

- intake: data/eval/intake.json 합성 자유서술 20개 → flag micro P/R/F1, victim_type 정확도.
- answer: 질문 10개(근거로 답할 수 있는 5 · 없는 5). 답한 것의 인용이 컨텍스트 안인지(유효성),
  기대 항목을 인용했는지, 근거 밖 질문을 '확인되지 않음'으로 돌려보냈는지.
- rewrite: 합성 서류 30개(data/eval/docs.json). 1단계(node tools/detect_cli.mjs)+2단계(/ner) span 을 넘겨
  고쳐 쓴 뒤 같은 1단계 탐지기로 다시 돌려 남은 단서 비율, 정답 span 원문이 남은 비율.

GPU 서버 주소는 --gpu-url(기본 ssh 터널 127.0.0.1:18000). 본문·질문·생성문은 출력·저장하지 않는다(id·지표만).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.eval import GpuDown, _f1, _load, _post, _ratio, run_tier1  # noqa: E402

ARTICLE_BASE_RE = re.compile(r"^(제\d+조(?:의\d+)?)")
NOT_FOUND = "근거에서 확인되지 않습니다"

# 근거 질의 10개. context 는 앱이 보낼 지식표 행 id. expect 는 답에 인용돼야 할 id(하나 이상).
QUESTIONS: list[dict[str, Any]] = [
    {"id": "a1", "answerable": True, "context": ["J1", "J2", "L3"], "expect": ["J1"],
     "q": "소송 서류에 제 주소가 상대에게 가지 않게 하려면 언제 신청해야 하나요?"},
    {"id": "a2", "answerable": True, "context": ["L3", "L4", "J2"], "expect": ["L3", "L4"],
     "q": "예전 집으로 가는 제 우편물을 새로 사는 곳으로 받을 수 있나요?"},
    {"id": "a3", "answerable": True, "context": ["S1", "S5", "S7"], "expect": ["S1"],
     "q": "가정폭력 때문에 이사했는데, 아이가 주소지가 아닌 곳의 학교에 다닐 수 있나요?"},
    {"id": "a4", "answerable": True, "context": ["L7", "D5"], "expect": ["L7"],
     "q": "남편 이름으로 된 휴대폰과 가족 요금제를 정리하려면 어떻게 하나요?"},
    {"id": "a5", "answerable": True, "context": ["P1", "P4"], "expect": ["P1"],
     "q": "집을 나왔는데 당장 머물 곳이 없어요. 어디에 도움을 청할 수 있나요?"},
    {"id": "n1", "answerable": False, "context": ["J1", "J7"], "expect": [],
     "q": "이혼하면 위자료는 보통 얼마나 받을 수 있나요?"},
    {"id": "n2", "answerable": False, "context": ["J19", "J23"], "expect": [],
     "q": "가해자는 보통 몇 년 형을 받나요?"},
    {"id": "n3", "answerable": False, "context": ["L3", "L4"], "expect": [],
     "q": "변호사를 선임하면 비용이 얼마나 드나요?"},
    {"id": "n4", "answerable": False, "context": ["A11", "S1"], "expect": [],
     "q": "결혼이민 체류 기간 연장은 어떻게 하나요?"},
    {"id": "n5", "answerable": False, "context": ["P7", "D6"], "expect": [],
     "q": "회사에서 병가를 며칠까지 쓸 수 있나요?"},
]

CTX_KEYS = ("path", "exposure", "measure", "timing", "where", "documents", "limits",
            "notify_warning", "status", "pending_effective", "plain")


def load_articles() -> dict[tuple[str, str], dict[str, Any]]:
    out = {}
    with (ROOT / "data/laws/articles.jsonl").open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                a = json.loads(line)
                out[(a["law"], a["article"])] = a
    return out


def build_item(item: dict[str, Any], arts: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    """앱이 /llm/answer 로 보낼 형태: 지식표 필드 + 노출 가능한 조문 발췌만."""
    it = {"id": item["id"]}
    for k in CTX_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v:
            it[k] = v[:500]
    it["articles"] = []
    for b in item.get("basis") or []:
        m = ARTICLE_BASE_RE.match(b.get("article", ""))
        a = arts.get((b.get("law"), m.group(1))) if m else None
        if not a or not a.get("exposable"):
            continue
        text = a.get("excerpt") or a.get("text") or ""
        it["articles"].append({"law": a["law"], "article": a["article"], "text": text[:600]})
        if len(it["articles"]) >= 2:
            break
    return it


def eval_intake(gpu: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = fn = 0
    vt_ok = 0
    per, lat = [], []
    for c in cases:
        t0 = time.time()
        r = _post(f"{gpu}/llm/intake", {"text": c["text"]}, timeout=120)
        lat.append(time.time() - t0)
        pred, gold = set(r.get("flags") or []), set(c["flags"])
        tp += len(pred & gold)
        fp += len(pred - gold)
        fn += len(gold - pred)
        ok = r.get("victim_type") == c["victim_type"]
        vt_ok += ok
        per.append({"id": c["id"], "victim_type_ok": ok, "pred_victim_type": r.get("victim_type"),
                    "gold_victim_type": c["victim_type"], "missed": sorted(gold - pred),
                    "extra": sorted(pred - gold), "n_unclear": len(r.get("unclear") or [])})
    p, rcl = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    return {"n": len(cases), "flag_micro": {"tp": tp, "fp": fp, "fn": fn, "precision": p,
                                            "recall": rcl, "f1": _f1(p, rcl)},
            "victim_type_accuracy": _ratio(vt_ok, len(cases)),
            "latency_s": {"mean": round(sum(lat) / len(lat), 2), "max": round(max(lat), 2)},
            "cases": per}


def eval_answer(gpu: str, knowledge: dict[str, Any]) -> dict[str, Any]:
    items = {i["id"]: i for i in knowledge["items"]}
    arts = load_articles()
    per, lat = [], []
    for q in QUESTIONS:
        ctx = [build_item(items[i], arts) for i in q["context"]]
        t0 = time.time()
        r = _post(f"{gpu}/llm/answer", {"question": q["q"], "items": ctx}, timeout=120)
        lat.append(time.time() - t0)
        cites = r.get("citations") or []
        answered = bool(r.get("grounded")) and not r.get("refused")
        per.append({
            "id": q["id"], "answerable": q["answerable"], "answered": answered,
            "refused_by_guard": bool(r.get("refused")),
            "not_found": NOT_FOUND in (r.get("answer") or ""),
            "citations": cites,
            "citations_in_context": all(c in q["context"] for c in cites),
            "cites_expected": bool(set(cites) & set(q["expect"])) if q["expect"] else None,
        })
    ans = [p for p in per if p["answerable"]]
    out = [p for p in per if not p["answerable"]]
    answered = [p for p in per if p["answered"]]
    return {
        "n": len(per),
        "answerable_answered": f"{sum(p['answered'] for p in ans)}/{len(ans)}",
        "answerable_cites_expected": f"{sum(bool(p['cites_expected']) for p in ans if p['answered'])}/{len(ans)}",
        "citation_validity": _ratio(sum(p["citations_in_context"] and bool(p["citations"]) for p in answered),
                                    len(answered)),
        "out_of_context_refusal": f"{sum((not p['answered']) and p['not_found'] for p in out)}/{len(out)}",
        "guard_refusals": sum(p["refused_by_guard"] for p in per),
        "latency_s": {"mean": round(sum(lat) / len(lat), 2), "max": round(max(lat), 2)},
        "note": "답변 본문의 근거 이탈(인용은 맞지만 내용이 근거 밖) 여부는 사람 판정 표본으로 따로 본다(SPEC §8).",
        "cases": per,
    }


def _residual(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 안전 문구('[… 비공개]')는 단서가 아니다.
    return [s for s in spans if "비공개" not in (s.get("text") or "")]


def eval_rewrite(gpu: str, docs: list[dict[str, Any]], node: str) -> dict[str, Any]:
    before = run_tier1(docs, node)
    rewritten: list[dict[str, Any]] = []
    per, lat = [], []
    refused = methods_rule = forced = 0
    for d in docs:
        # 실제 흐름처럼 1단계(브라우저 정규식) + 2단계(/ner) span 을 함께 넘긴다.
        found = before[d["id"]] + (_post(f"{gpu}/ner", {"text": d["text"]}).get("spans") or [])
        spans = [{"start": s["start"], "end": s["end"], "label": s["label"]} for s in found]
        t0 = time.time()
        r = _post(f"{gpu}/llm/rewrite", {"text": d["text"], "spans": spans}, timeout=180)
        lat.append(time.time() - t0)
        if r.get("refused") or not r.get("suggested"):
            refused += 1
            per.append({"id": d["id"], "refused": True})
            continue
        methods_rule += r.get("method") == "rule"
        forced += int(r.get("forced") or 0)
        rewritten.append({"id": d["id"], "text": r["suggested"], "gold": d["spans"]})
    after = run_tier1(rewritten, node) if rewritten else {}
    n_before = sum(len(before[d["id"]]) for d in docs if d["id"] in after)
    n_after = 0
    gold_total = gold_left = docs_with_residual = 0
    for rw in rewritten:
        left = _residual(after[rw["id"]])
        n_after += len(left)
        docs_with_residual += bool(left)
        g_left = sum(1 for g in rw["gold"] if len(g["text"].strip()) >= 2 and g["text"].strip() in rw["text"])
        gold_total += len(rw["gold"])
        gold_left += g_left
        per.append({"id": rw["id"], "refused": False, "tier1_before": len(before[rw["id"]]),
                    "tier1_after": len(left), "gold_spans": len(rw["gold"]), "gold_left": g_left})
    return {
        "n": len(docs), "rewritten": len(rewritten), "refused": refused,
        "rule_fallback": methods_rule, "forced_masks": forced,
        "tier1_spans_before": n_before, "tier1_spans_after": n_after,
        "tier1_residual_rate": _ratio(n_after, n_before),
        "docs_with_tier1_residual": f"{docs_with_residual}/{len(rewritten)}",
        "gold_span_text_left_rate": _ratio(gold_left, gold_total),
        "latency_s": {"mean": round(sum(lat) / len(lat), 2), "max": round(max(lat), 2)},
        "cases": sorted(per, key=lambda p: p["id"]),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu-url", default="http://127.0.0.1:18000")
    ap.add_argument("--node", default="node")
    ap.add_argument("--out", default=str(ROOT / "data/eval/results_llm.json"))
    a = ap.parse_args()
    gpu = a.gpu_url.rstrip("/")
    try:
        health = _post(f"{gpu}/llm/answer", {"question": "점검", "items": []}, timeout=10)
        del health
        res = {
            "generated_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": "qwen3-8b (Qwen/Qwen3-8B-AWQ, vLLM, thinking off)",
            "intake": eval_intake(gpu, _load("data/eval/intake.json")["cases"]),
            "answer": eval_answer(gpu, _load("data/knowledge.json")),
            "rewrite": eval_rewrite(gpu, _load("data/eval/docs.json"), a.node),
        }
    except GpuDown as exc:
        print(f"GPU/LLM 불가: {exc}", file=sys.stderr)
        return 2
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    i, an, rw = res["intake"], res["answer"], res["rewrite"]
    print(f"intake  flag F1 {i['flag_micro']['f1']} (P {i['flag_micro']['precision']} R {i['flag_micro']['recall']})"
          f" · victim_type acc {i['victim_type_accuracy']} · {i['latency_s']['mean']}s/건")
    print(f"answer  답변 {an['answerable_answered']} · 기대 인용 {an['answerable_cites_expected']}"
          f" · 인용 유효 {an['citation_validity']} · 근거 밖 거절 {an['out_of_context_refusal']}"
          f" · 가드 거절 {an['guard_refusals']}")
    print(f"rewrite 1단계 잔존 {rw['tier1_spans_after']}/{rw['tier1_spans_before']} ({rw['tier1_residual_rate']})"
          f" · 잔존 문서 {rw['docs_with_tier1_residual']} · 정답 원문 잔존 {rw['gold_span_text_left_rate']}"
          f" · 거절 {rw['refused']} · 규칙 대체 {rw['rule_fallback']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
