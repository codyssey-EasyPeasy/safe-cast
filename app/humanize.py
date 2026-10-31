"""사용자에게 보이는 지식표 문구 다듬기(2026-10-31 리뷰 반영).

- 내부 항목 id(A1·J1 등)를 제도 이름으로 바꾼다. 지식표는 research 문서에서 빌드되므로
  원본을 고치지 않고 적재할 때 바꾼다(재빌드해도 유지).
- 같은 신청 안에서 함께 처리되는 항목은 통지 경고를 물려받는다(NOTIFY_INHERIT).
- 근거 표시용 이름(`source_label`)을 만든다: '제도 이름 · 법령 조문'.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# 다른 항목이 본문에서 가리키는 id 의 짧은 이름(사람이 읽는 이름).
SHORT_NAME: dict[str, str] = {
    "A1": "주민등록 열람·교부 제한",
    "A6": "세대 분리",
    "A8": "가족관계 증명서 교부 제한",
    "J1": "소송관계인 개인정보 보호조치",
    "J8": "보호명령 청구서 주소 가림",
    "J9": "결정서 주거 기재 생략",
    "J10": "접근금지 변경 신청",
    "S8": "친권 제한",
}

# 같은 방문·같은 서식으로 함께 신청하는 항목: 원 항목의 통지 경고를 그대로 보여 준다.
# A6(전입과 같은 방문에서 하는 제한 신청) → A1 의 서면 통지, A9(공시제한, A8 과 같은 서식) → A8 의 고지.
NOTIFY_INHERIT: dict[str, str] = {"A6": "A1", "A9": "A8"}

USER_FIELDS = ("path", "exposure", "measure", "timing", "where", "documents", "limits",
               "notify_warning", "plain")

_ID_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]\d{1,3})(?![0-9A-Za-z])")
_PAREN_ID_RE = re.compile(r"\(([A-Z]\d{1,3})\)")
_BRACKET_ID_RE = re.compile(r"\s?\[([A-Z]\d{1,3})(?:\s*,\s*[A-Z]\d{1,3})*\]")

# 받침 유무에 따라 바꿔야 하는 조사(받침 있음 → 없음).
_PARTICLE = {"과": "와", "은": "는", "을": "를", "이": "가", "으로": "로"}
_PARTICLE_REV = {v: k for k, v in _PARTICLE.items()}


def _has_batchim(word: str) -> bool:
    for ch in reversed(word):
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3:
            return (o - 0xAC00) % 28 != 0
        if ch.isalnum():
            return False
    return False


def default_name(item: dict[str, Any]) -> str:
    if item.get("status") == "none":
        base = item.get("path") or ""
    else:
        base = item.get("measure") or item.get("path") or ""
    name = re.split(r"[(.,]", base, maxsplit=1)[0].strip()
    return name[:40] or (item.get("id") or "")


def name_map(items: list[dict[str, Any]]) -> dict[str, str]:
    out = {it["id"]: default_name(it) for it in items if it.get("id")}
    out.update({k: v for k, v in SHORT_NAME.items() if k in out})
    return out


def humanize_text(text: str, names: dict[str, str]) -> str:
    """본문 속 id 를 이름으로 바꾼다. '(A6)' 처럼 괄호만 있는 id 는 앞말이 이미 이름이라 지운다."""
    if not text or not _ID_RE.search(text):
        return text
    text = _PAREN_ID_RE.sub(lambda m: "" if m.group(1) in names else m.group(0), text)
    out: list[str] = []
    pos = 0
    for m in _ID_RE.finditer(text):
        iid = m.group(1)
        if iid not in names or m.start() < pos:
            continue
        name = names[iid]
        out.append(text[pos:m.start()])
        rest = text[m.end():]
        # 'A1 제한', 'J1 개인정보 보호조치' 처럼 이름의 끝 낱말(들)이 바로 뒤에 다시 오면 겹치지 않게 뺀다.
        words = name.split()
        skip = 0
        for k in range(len(words), 0, -1):
            suffix = " ".join(words[-k:])
            if rest.startswith(" " + suffix):
                skip = 1 + len(suffix)
                break
        out.append(name)
        pos = m.end() + skip
        # 조사 맞추기.
        tail = text[pos:pos + 2]
        for p in sorted(_PARTICLE, key=len, reverse=True) + sorted(_PARTICLE_REV, key=len, reverse=True):
            if tail.startswith(p) and (len(tail) == len(p) or not ("가" <= tail[len(p):len(p) + 1] <= "힣")):
                want = p
                if _has_batchim(name) and p in _PARTICLE_REV:
                    want = _PARTICLE_REV[p]
                elif not _has_batchim(name) and p in _PARTICLE:
                    want = _PARTICLE[p]
                out.append(want)
                pos += len(p)
                break
    out.append(text[pos:])
    return re.sub(r"\s{2,}", " ", "".join(out))


def strip_bracket_ids(text: str) -> str:
    """생성문 끝의 '[A1]' 같은 내부 인용 표시를 지운다."""
    return _BRACKET_ID_RE.sub("", text or "").strip()


def source_label(item: dict[str, Any], names: dict[str, str]) -> str:
    name = names.get(item.get("id", ""), default_name(item))
    basis = item.get("basis") or []
    prim = next((b for b in basis if b.get("primary")), basis[0] if basis else None)
    if prim:
        law = f"{prim.get('law', '')} {prim.get('article', '')}".strip()
        if law:
            return f"{name} · {law}"
    return name


def prepare_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """적재 시 한 번: id → 이름, 통지 경고 물려받기. 원본 리스트는 바꾸지 않는다."""
    names = name_map(items)
    by_id = {it.get("id"): it for it in items}
    out: list[dict[str, Any]] = []
    for it in items:
        c = copy.deepcopy(it)
        for k in USER_FIELDS:
            v = c.get(k)
            if isinstance(v, str):
                c[k] = humanize_text(v, names)
        for cn in c.get("conditional_notes") or []:
            if isinstance(cn.get("note"), str):
                cn["note"] = humanize_text(cn["note"], names)
        src = NOTIFY_INHERIT.get(c.get("id", ""))
        if src and not c.get("notify_warning") and by_id.get(src, {}).get("notify_warning"):
            c["notify_warning"] = (
                f"이 신청은 '{names.get(src, src)}' 신청과 함께 합니다. "
                + humanize_text(by_id[src]["notify_warning"], names)
            )
            c["notify_inherited_from"] = src
        out.append(c)
    return out
