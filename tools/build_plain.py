#!/usr/bin/env python3
"""`plain` 필드 빌드 도우미 (docs/SPEC.md §2).

스스로 LLM·네트워크를 호출하지 않는다. 운영자가 `export` 로 뽑은 프롬프트를
LLM 에 보내고, 그 응답 JSON 을 `apply` 로 되먹인다.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KNOWLEDGE = ROOT / "data" / "knowledge.json"
ARTICLES_PATH = ROOT / "data" / "laws" / "articles.jsonl"

PROMPT_TEMPLATE = (
    "You write plain-Korean explanations for counselors who help domestic violence / stalking / "
    "sexual violence survivors protect a NEW address from the abuser. For EACH item write 2-3 short "
    "Korean sentences (polite 해요체, max 220 characters) explaining what the protective MEASURE does, "
    "who can use it (victim_types: family=가정폭력, stalking=스토킹, dating=교제폭력, sexual=성폭력, "
    "any=유형 무관) and its main limit. Only describe protective/blocking measures and public legal "
    "information. NEVER describe how to find, locate, look up or obtain records about a person. "
    "If status is 'none', say plainly that there is no blocking system and what to be careful about "
    "beforehand. If status is 'pending', say it takes effect on pending_effective and cannot be used "
    "yet. Do not invent article numbers, dates, fees or phone numbers not present in the item. "
    "Return ONLY a JSON object mapping id -> explanation string, no markdown."
)

BASE_ARTICLE_RE = re.compile(r"^(제\d+조(?:의\d+)?)")
HANGUL_RE = re.compile(r"[가-힣]")
BANNED = (
    "조회하는 방법",
    "찾는 방법",
    "알아내는 방법",
    "알아내려면",
    "찾으려면",
    "위치를 찾",
    "주소를 찾",
    "추적하는 방법",
    "열람하려면",
    "발급받으려면",
)


def load_knowledge(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_knowledge(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )


def load_articles() -> list[dict]:
    if not ARTICLES_PATH.exists():
        return []
    rows: list[dict] = []
    with ARTICLES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def find_excerpt(articles: list[dict], law: str | None, article: str | None) -> str | None:
    """basis 의 (law, article) 에 해당하는 조문 본문 앞 500자."""
    m = BASE_ARTICLE_RE.match(article or "")
    if not m:
        return None
    base = m.group(1)
    for row in articles:
        if row.get("law") != law:
            continue
        art = str(row.get("article") or "")
        if art == base or art.startswith(base):
            return str(row.get("text") or "")[:500]
    return None


def build_record(item: dict, articles: list[dict]) -> dict:
    basis = item.get("basis") or []
    excerpts: list[str] = []
    for b in basis:
        excerpt = find_excerpt(articles, b.get("law"), b.get("article"))
        if excerpt is not None:
            excerpts.append(excerpt)
    return {
        "id": item.get("id"),
        "measure": item.get("measure", ""),
        "path": item.get("path", ""),
        "where": item.get("where", ""),
        "limits": item.get("limits", ""),
        "status": item.get("status", ""),
        "pending_effective": item.get("pending_effective"),
        "victim_types": item.get("victim_types", []),
        "basis": [{"law": b.get("law"), "article": b.get("article")} for b in basis],
        "article_excerpts": excerpts,
    }


def cmd_export(args: argparse.Namespace, knowledge_path: Path) -> int:
    data = load_knowledge(knowledge_path)
    articles = load_articles()
    items = data.get("items", [])

    records: list[dict] = []
    for item in items:
        if args.only_empty and (item.get("plain") or "").strip():
            continue
        records.append(build_record(item, articles))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not records:
        print("exported 0 items")
        return 0

    batch_size = max(1, args.batch_size)
    batches = 0
    for start in range(0, len(records), batch_size):
        batches += 1
        chunk = records[start : start + batch_size]
        stem = f"batch_{batches:02d}"
        (out_dir / f"{stem}.json").write_text(
            json.dumps(chunk, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        prompt = (
            PROMPT_TEMPLATE
            + "\n\nITEMS:\n"
            + json.dumps(chunk, ensure_ascii=False, indent=1)
        )
        (out_dir / f"{stem}.prompt.txt").write_text(prompt + "\n", encoding="utf-8")

    print(f"exported {len(records)} items in {batches} batch(es) to {out_dir}")
    return 0


def extract_object(text: str) -> dict | None:
    """```json 펜스나 앞뒤 잡설이 섞여도 가장 바깥 {...} 를 뽑아낸다."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def validate_text(text: object) -> str | None:
    if not isinstance(text, str):
        return "not a string"
    s = text.strip()
    if len(s) < 20:
        return "too short"
    if len(s) > 400:
        return "too long"
    if not HANGUL_RE.search(s):
        return "no hangul"
    for banned in BANNED:
        if banned in s:
            return f"banned phrase: {banned}"
    return None


def cmd_apply(args: argparse.Namespace, knowledge_path: Path) -> int:
    data = load_knowledge(knowledge_path)
    items = data.get("items", [])
    by_id = {item.get("id"): item for item in items}

    accepted = rejected = skipped = 0
    for resp in args.responses:
        path = Path(resp)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"REJECT {path} {exc}")
            rejected += 1
            continue
        obj = extract_object(raw)
        if obj is None:
            print(f"REJECT {path} no JSON object")
            rejected += 1
            continue

        for item_id, text in obj.items():
            item = by_id.get(item_id)
            if item is None:
                print(f"REJECT {item_id} unknown id")
                rejected += 1
                continue
            if item.get("plain_reviewed") is True and not args.force:
                print(f"REJECT {item_id} already reviewed (use --force)")
                skipped += 1
                continue
            reason = validate_text(text)
            if reason is not None:
                print(f"REJECT {item_id} {reason}")
                rejected += 1
                continue
            item["plain"] = text.strip()
            item["plain_reviewed"] = False
            accepted += 1

    write_knowledge(knowledge_path, data)

    empty_ids = [
        item.get("id") for item in items if not (item.get("plain") or "").strip()
    ]
    print(f"accepted {accepted}, rejected {rejected}, skipped {skipped}")
    print(f"empty plain: {len(empty_ids)}")
    for item_id in empty_ids:
        print(f"  {item_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="knowledge.json plain 필드 빌드 도우미")
    parser.add_argument(
        "--knowledge",
        default=str(DEFAULT_KNOWLEDGE),
        help="knowledge.json 경로 (기본 data/knowledge.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export_p = sub.add_parser("export", help="LLM 프롬프트 배치 내보내기")
    export_p.add_argument("--out", required=True, metavar="DIR")
    export_p.add_argument("--batch-size", type=int, default=12)
    export_p.add_argument("--only-empty", action="store_true")

    apply_p = sub.add_parser("apply", help="LLM 응답 JSON 반영")
    apply_p.add_argument("responses", nargs="+", metavar="RESP_JSON")
    apply_p.add_argument("--force", action="store_true")

    return parser


def main() -> int:
    args = build_parser().parse_args()
    knowledge_path = Path(args.knowledge)
    if args.command == "export":
        return cmd_export(args, knowledge_path)
    if args.command == "apply":
        return cmd_apply(args, knowledge_path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
