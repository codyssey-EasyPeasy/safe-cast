#!/usr/bin/env python3
"""data/knowledge.json 의 basis 조문을 law.go.kr 에서 받아 data/laws/articles.jsonl 로 스냅샷한다.

빌드 도구. 런타임 아님. 표준 라이브러리만 사용.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

UA = "Mozilla/5.0 (sumeunjip build)"
SLEEP = 0.7
TIMEOUT = 30
RETRIES = 2

BASE_RE = re.compile(r"^(제\d+조(?:의\d+)?)")
HEADING_RE = re.compile(r"^제\d+조(의\d+)? ?\(")

# 조문별 링크(/법령/<이름>/<조>)가 없는 행정규칙·시행 예정본: 전문 페이지에서 조문을 잘라 낸다.
FULLBODY = {
    "가족관계등록예규 제653호": "https://www.law.go.kr/LSW/admRulInfoR.do?admRulSeq=2200000109345&chrClsCd=010201",
    "피해자 보호 및 지원에 관한 규칙(경찰청훈령)": "https://www.law.go.kr/LSW/admRulInfoR.do?admRulSeq=2100000273166&chrClsCd=010201",
    "스토킹범죄의 처벌 등에 관한 법률(법률 제21556호)": "https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq=285471&efYd=20270422&chrClsCd=010202&ancYnChk=0",
}
EFFECTIVE_RE = re.compile(r"\[시행\s*(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\]")
IFRAME_RE = re.compile(r'src="(/LSW//?lsSideInfoP\.do\?[^"]+)"')
LSISEQ_RE = re.compile(r"lsiSeq=(\d+)")
SCRIPT_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
CIRCLED_RE = re.compile("[" + CIRCLED + "]")
ITEM_RE = re.compile(r"(?<=\s)(\d+\.\s)")

_last_req = 0.0


class FetchError(Exception):
    pass


def _throttle() -> None:
    global _last_req
    now = time.monotonic()
    delta = SLEEP - (now - _last_req)
    if delta > 0:
        time.sleep(delta)
    _last_req = time.monotonic()


def _cache_path(cache_dir: Path, url: str) -> Path:
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return cache_dir / (key + ".html")


def http_get(url: str, cache_dir: Path) -> str:
    cp = _cache_path(cache_dir, url)
    if cp.exists():
        return cp.read_text(encoding="utf-8", errors="replace")
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            cache_dir.mkdir(parents=True, exist_ok=True)
            cp.write_text(text, encoding="utf-8")
            return text
        except Exception as e:  # noqa: BLE001
            last = e
    raise FetchError(f"{url}: {last}")


BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|tr|td|th|table|h\d)\b[^>]*>", re.I)


def html_to_text(raw: str) -> str:
    """Block tags become line breaks; inline tags (a/span/label) are removed so that
    linked terms stay inside their sentence."""
    s = SCRIPT_RE.sub(" ", raw)
    s = BLOCK_TAG_RE.sub("\n", s)
    s = TAG_RE.sub("", s)
    s = html.unescape(s)
    out: list[str] = []
    for line in s.split("\n"):
        line = re.sub(r"[ \t\r\f\v\u00a0]+", " ", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def parse_effective(text: str) -> str:
    m = EFFECTIVE_RE.search(text)
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def extract_article(lines: list[str], base_article: str) -> str:
    start = None
    for i, line in enumerate(lines):
        if line.startswith(base_article + "(") or line.startswith(base_article + " ("):
            start = i
            break
    if start is None:
        return ""
    collected: list[str] = []
    for line in lines[start:]:
        if line == "위로":
            break
        if collected and HEADING_RE.match(line):
            m = BASE_RE.match(line)
            if m and m.group(1) != base_article:
                break
        collected.append(line)
    # 조문 끝에 붙은 장·절·편 제목은 뗀다.
    while collected and re.match(r"^제\d+(?:장|절|편|관)(?:\s|$)", collected[-1]):
        collected.pop()
    return "\n".join(collected).strip()


def fetch_fullbody(law: str, base_article: str, cache_dir: Path) -> dict:
    url = FULLBODY[law]
    text = html_to_text(http_get(url, cache_dir))
    body = extract_article(text.split("\n"), base_article)
    if not body.startswith(base_article) or len(body) < 20:
        raise FetchError("article text invalid")
    m = LSISEQ_RE.search(url)
    return {"law": law, "article": base_article, "effective": parse_effective(text),
            "lsiSeq": m.group(1) if m else None, "url": url, "text": body}


def fetch_pair(law: str, base_article: str, cache_dir: Path) -> dict:
    if law in FULLBODY:
        return fetch_fullbody(law, base_article, cache_dir)
    candidates = [law, law.replace(" ", "")]
    iframe_src: str | None = None
    used_name: str | None = None
    lsi_seq: str | None = None

    for name in candidates:
        if not name:
            continue
        name_url = (
            "https://www.law.go.kr/"
            + urllib.parse.quote("법령")
            + "/"
            + urllib.parse.quote(name)
            + "/"
            + urllib.parse.quote(base_article)
        )
        try:
            raw = http_get(name_url, cache_dir)
        except FetchError:
            continue
        m = IFRAME_RE.search(raw)
        if not m:
            continue
        iframe_src = html.unescape(m.group(1))
        used_name = name
        lsi_m = LSISEQ_RE.search(iframe_src)
        lsi_seq = lsi_m.group(1) if lsi_m else None
        break

    if iframe_src is None or used_name is None:
        raise FetchError("iframe src not found")

    raw2 = http_get("https://www.law.go.kr" + iframe_src, cache_dir)
    text = html_to_text(raw2)
    effective = parse_effective(text)
    body = extract_article(text.split("\n"), base_article)
    if not body.startswith(base_article) or len(body) < 20:
        raise FetchError("article text invalid")

    human_url = f"https://www.law.go.kr/법령/{used_name}/{base_article}"
    return {
        "law": law,
        "article": base_article,
        "effective": effective,
        "lsiSeq": lsi_seq,
        "url": human_url,
        "text": body,
    }


def load_pairs(knowledge_path: Path) -> dict[tuple[str, str], dict]:
    data = json.loads(knowledge_path.read_text(encoding="utf-8"))
    pairs: dict[tuple[str, str], dict] = {}
    for item in data.get("items", []) or []:
        for b in item.get("basis", []) or []:
            law = (b.get("law") or "").strip()
            art = (b.get("article") or "").strip()
            if not law or not art:
                continue
            m = BASE_RE.match(art)
            if not m:
                continue
            key = (law, m.group(1))
            if key in pairs:
                continue
            pairs[key] = {"url": (b.get("url") or "").strip()}
    return pairs


def load_fallback(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], dict] = {}
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        law = entry.get("law")
        art = entry.get("article")
        if not law or not art:
            continue
        m = BASE_RE.match(art)
        if not m:
            continue
        out[(law, m.group(1))] = entry
    return out


def _fallback_record(
    law: str, base: str, src_url: str, fb: dict
) -> dict:
    return {
        "law": law,
        "article": base,
        "effective": fb.get("effective") or "",
        "lsiSeq": None,
        "url": fb.get("url") or src_url,
        "text": fb.get("text") or "",
        "partial": True,
    }


def _should_skip(law: str, src_url: str) -> bool:
    if law in FULLBODY:
        return False
    return "(법률 제21556호)" in law or "efYd=2027" in src_url


def _write_out(records: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    records.sort(key=lambda r: (r["law"], r["article"]))
    with out_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--knowledge", default=str(ROOT / "data/knowledge.json")
    )
    parser.add_argument(
        "--fallback", default=str(ROOT / "data/laws/fallback_quotes.json")
    )
    parser.add_argument("--cache", default=str(ROOT / ".cache/law"))
    parser.add_argument("--out", default=str(ROOT / "data/laws/articles.jsonl"))
    parser.add_argument("--only", default=None)
    args = parser.parse_args()

    cache_dir = Path(args.cache)
    out_path = Path(args.out)
    pairs = load_pairs(Path(args.knowledge))
    fallback = load_fallback(Path(args.fallback))

    counts = {"OK": 0, "PARTIAL": 0, "FAIL": 0}
    records: list[dict] = []

    for key in sorted(pairs.keys(), key=lambda k: (k[0], k[1])):
        law, base = key
        if args.only and args.only not in law:
            continue
        src_url = pairs[key]["url"]
        fb = fallback.get((law, base))

        if _should_skip(law, src_url):
            if fb:
                records.append(_fallback_record(law, base, src_url, fb))
                counts["PARTIAL"] += 1
                print(f"PARTIAL {law} {base}")
            else:
                counts["FAIL"] += 1
                print(f"FAIL {law} {base}")
                print(
                    f"  skip-future: {law} {base} (fallback 없음)",
                    file=sys.stderr,
                )
            continue

        try:
            rec = fetch_pair(law, base, cache_dir)
            records.append(rec)
            counts["OK"] += 1
            print(f"OK {law} {base}")
        except Exception as e:  # noqa: BLE001
            if fb:
                records.append(_fallback_record(law, base, src_url, fb))
                counts["PARTIAL"] += 1
                print(f"PARTIAL {law} {base}")
            else:
                counts["FAIL"] += 1
                print(f"FAIL {law} {base}")
            print(f"  reason: {e}", file=sys.stderr)

    _write_out(records, out_path)
    print(
        "summary "
        f"OK={counts['OK']} "
        f"PARTIAL={counts['PARTIAL']} "
        f"FAIL={counts['FAIL']} "
        f"total={counts['OK'] + counts['PARTIAL'] + counts['FAIL']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
