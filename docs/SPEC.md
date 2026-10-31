# 보안 예보 구현 명세 (v1, 2026-10-31 · 2026-10-31 18시 실제 구현 반영)

마감 2026-11-01 13:00 KST. 아키텍처: `architecture.md`. 근거: `../research/2026-10-31-정보조사.md`. 실증 자료 색인: `EVIDENCE.md`.

> **읽는 법.** §0~§10 은 결정한 순서대로 남긴 설계 기록이다. 나중 결정으로 바뀐 줄은 지우지 않고 `[대체됨 → §11.x]` 표시를 붙였다.
> 지금 배포된 동작(커밋 `2418ad9` 코드, 2026-10-31 17:57 재기동)은 **§11 이 정본**이다.

## 0. 공통 규칙
- Python 3.12, `uv` 사용. 런타임 의존성 최소화. 한국어 UI·메시지.
- **사례 정보 비저장·비로깅**: 요청 본문을 로그에 남기지 않는다(접근 로그는 시각·경로·상태코드만). DB 없음.
- 외부 LLM 호출은 `tools/build_*` 빌드 스크립트에서만. `[보충 → §11.7: 합성 카드 시뮬레이터·레드팀 도구(`tools/sim/`)도 중계 경유 GLM 을 쓴다. 런타임은 그대로 0]` `app/`·`gpu/`·`web/` 런타임 코드는 외부 인터넷을 호출하지 않는다(GPU 서버 모델 최초 다운로드 제외).
- 막는 제도만 다룬다. 사람·주소를 찾는 방법, 조회 요건·절차를 출력하는 텍스트를 만들지 않는다.
- 모든 데이터는 합성 또는 공개 법령.

## 1. 디렉터리
```
data/knowledge.json        # 지식표 (Lane K)
data/laws/*.jsonl          # 법령 조문 스냅샷 (Lane K)
data/eval/*.json           # 합성 시나리오·서류·레드팀 (Lane D)
app/                       # FastAPI 운영 서버 (Lane E)  :8080
gpu/                       # FastAPI 추론 서버 (Lane G)  :8000
web/                       # 정적 프런트엔드 (Lane F), app 이 / 로 서빙
tools/                     # 빌드·평가 스크립트
tests/                     # pytest
```

## 2. 지식표 스키마 `data/knowledge.json`
```json
{
  "version": "2026-10-31",
  "flags": [{"id": "moved_recently", "label": "최근 이사(또는 이사 예정)", "group": "주거"}],
  "items": [{
    "id": "J1",
    "area": "소송·수사",                       // 행정 | 소송·수사 | 학적·보육 | 차량·우편·통신 | 디지털·위치 | 피신·지원
    "path": "민사·가사 소장·서면·판결의 송달",
    "exposure": "당사자 표시의 주소가 상대방에게 송달된다",   // 방어 관점 한 문장, 조회 방법 금지
    "measure": "소송관계인 개인정보 보호조치(주소 등 비공개)",
    "basis": [{"law": "민사소송법", "article": "제163조 제2항", "effective": "2025-07-12", "url": "https://www.law.go.kr/...", "primary": true}],
    "victim_types": ["family", "stalking", "dating", "sexual", "any"],   // 이 제도를 쓸 수 있는 피해 유형. "any" = 유형 무관
    "triggers": {"all": ["lawsuit"], "any": []},   // 상황 flag 조건. all 은 모두, any 는 하나 이상(빈 배열이면 무조건)
    "urgency": 1,                       // 1 = 되돌릴 수 없는 시점(송달 전, 전입 시) · 2 = 빨리 · 3 = 여유
    "timing": "소장 접수 때 함께",
    "where": "수소법원에 서면 신청",
    "documents": "신청서, 위해 소명자료",
    "limits": "신청주의. 가사비송 적용 미확인",
    "notify_warning": null,             // 문자열이면 '신청 사실이 상대에게 알려짐' 경고문
    "status": "in_force",               // in_force | pending(시행 예정, effective 날짜) | none(막을 제도 없음 = 잔여 위험)
    "pending_effective": null,
    "confidence": "확인",               // 확인 | 부분확인 | 미확인
    "plain": "",                        // 쉬운 말 설명(빌드 생성, 사람 검수 전이면 plain_reviewed=false)
    "plain_reviewed": false
  }]
}
```
피해 유형 id: `family`(가정폭력, 배우자·사실혼·전 배우자·직계존비속·동거친족), `stalking`, `dating`(교제폭력, 법률 정의 없음), `sexual`(성폭력).
상황 flag(최소 세트, Lane K 가 확정): `moved_recently`, `same_registration_address`(가해자와 주민등록지 같음), `lawsuit`(민사·가사 소송 중/예정), `abuser_is_plaintiff`, `criminal_case`(고소·수사 중), `protection_order`(접근금지 등 조치 있음/예정), `children_school`, `children_daycare`, `vehicle`, `shared_vehicle_or_abuser_owned`, `own_property_or_lease_registration`, `company_officer`, `mail_old_address`, `parcel_delivery`, `abuser_named_phone_or_family_plan`, `shared_accounts_or_location_sharing`, `bluetooth_tag_suspected`, `photos_social`, `foreign_national`, `health_insurance_dependent`, `shelter_needed`, `workplace_known`, `election_period`.

## 3. 운영 API (`app/`, :8080)
- `GET /` → `web/index.html`. 정적 파일 `/static/*`.
- `POST /api/diagnose` 요청 `{"victim_type": "stalking", "flags": ["moved_recently","lawsuit"]}` (그 밖의 필드는 422)
  응답 `{"checklist": [item...], "residual": [item...], "not_applicable": [{"id","measure","reason"}], "summary": {...}}`
  - checklist: status in_force/pending 이고 triggers 충족·victim_types 에 해당(또는 any). 정렬 urgency → area 순서 → id.
  - pending 은 `"note": "YYYY-MM-DD 시행 예정, 지금은 쓸 수 없음"` 표시하고 checklist 맨 뒤.
  - residual: status none 이고 triggers 충족.
  - not_applicable: triggers 충족하지만 victim_type 이 대상이 아님 → 공백을 숨기지 않고 "이 유형엔 제도 없음" 표시(예: 스토킹 피해자에게 주민등록 교부 제한).
- `POST /api/search` `{"q": "..."}` `[거절·애매 응답 형식 대체됨 → §11.1]` → 가드(의도 분류) 통과 시 GPU `/embed` + `/rerank` 로 법령 조문 상위 5개 `{law, article, effective, text, url, score}`. 거절 시 `{"refused": true, "message": "..."}`. GPU 불가 시 키워드(BM25 간이) 폴백, `"fallback": true`.
- `POST /api/doccheck` `{"text": "..."}` → GPU `/ner` 결과(직장·학교·생활권 단서 span)만 반환. 본문 로그 금지. GPU 불가 시 `{"available": false}`.
- `GET /api/health`.
- 설정: 환경변수 `GPU_URL`(기본 `http://10.1.2.33:8000`), `ACCESS_CODE`(상담원 기관 코드, 헤더 `X-Access-Code`; 미설정이면 데모 모드로 통과). `[대체됨 → §7: 지금은 `REQUIRE_ACCESS_CODE=1` 일 때만 검사, 그때 `ACCESS_CODE` 가 비면 401(fail-closed)]`

## 4. GPU 추론 API (`gpu/`, :8000)
- `POST /embed` `{"texts": [...]}` → `{"vectors": [[...]]}` (BAAI/bge-m3 dense, 정규화)
- `POST /rerank` `{"query": "...", "docs": [...]}` → `{"scores": [...]}` (BAAI/bge-reranker-v2-m3)
- `POST /ner` `{"text": "..."}` → `{"spans": [{"start","end","label","text","score"}]}` label ∈ {주소, 직장, 학교, 차량, 생활권}. 모델: GLiNER 다국어(urchade/gliner_multi-v2.1) 우선, 실패 시 규칙 폴백.
- `POST /intent` `{"q": "..."}` → `{"locate_intent": bool, "score": float}`. 시드 문장 임베딩 유사도 + 규칙. 시드는 `gpu/intent_seeds.json`(locate / protect 양쪽).
- 본문 로그 금지. 요청 크기 상한 20KB.

## 5. 프런트엔드 (`web/`)
- 단일 페이지, 바닐라 JS, 외부 CDN 없이도 동작(폰트는 시스템 폰트 폴백).
- 1) 피해 유형 선택 2) 상황 체크(그룹별 체크박스) 3) 결과: 급한 순 체크리스트 카드(제도·근거 조문·시행일·대상·신청처·서류·한계·확인도 배지·**통지 경고 노란 박스**·pending 회색), 잔여 위험 섹션, '이 유형엔 제도 없음' 섹션, 인쇄 버튼 4) 서류 점검: 텍스트 붙여넣기 → 브라우저 정규식 1단계(주소·전화·주민번호·차량번호·이메일·학교명·직장 단서 키워드) 하이라이트 + 대체 문구 제안, "기관 내부 2단계 점검" 버튼은 /api/doccheck.
- 상단 고정 '빠른 나가기' 버튼(즉시 상태 초기화 + 중립 사이트로 location.replace). localStorage·쿠키 사용 금지. 새로고침 시 초기화.
- 첫 화면 고지: "법률정보 안내이며 법률상담이 아닙니다. 긴급 112 · 1366 · 법률구조공단 132". AI 생성 설명엔 'AI 생성(검수 전)' 배지.
- 근거 질의 상자(/api/search), 거절 시 안내문.

## 6. 평가 (`tools/eval.py` → `data/eval/results.json`)
- 시나리오 20개(`scenarios.json`: victim_type, flags, must_include[ids], must_not_include[ids]) → 진단 재현율, 대상 오적용 수.
- 합성 서류 30개(`docs.json`: text, spans[label,start,end]) → 1단계(정규식, JS 로직을 Python 으로 동일 구현하거나 node 실행)·2단계(NER) 정밀도/재현율.
- 레드팀 40개(`redteam.json`: q, expect_refuse) → 거절률·오거절률.
- 근거 정확도: knowledge.json basis 중 data/laws 스냅샷에 있는 조문 비율.

## 7. 변경 (2026-10-31 오너 결정): 상담사 경유 제거 → 일반 사용자(피해자 본인) 직접 사용
- 대상 사용자: 피해자 본인과 가족·지인. 상담원 전용 전제와 `ACCESS_CODE` 인증은 쓰지 않는다(설정돼 있어도 기본 비활성).
- 화면 문구는 사용자에게 직접 말하는 쉬운 존댓말("~하실 수 있어요"보다 "~할 수 있습니다" 수준의 차분한 안내). '상담원' 표현 제거.
- 첫 화면 '안전한 기기' 안내: 상대가 볼 수 있는 기기·계정이면 다른 기기 사용 권장, 시크릿(개인정보 보호) 창 권장, 빠른 나가기 위치 안내. 방문 기록이 남을 수 있음을 알림.
- 결과마다 "혼자 하기 어려우면 1366(24시간) · 법률구조공단 132" 연결을 붙인다. 위험이 급하면 112.
- 이중용도 강화: 잔여 위험(제도 없는 경로)은 "이 기록에는 주소가 남을 수 있으니 등록·신청 전에 고려하세요" 수준의 사전 안내만, 조회 주체·요건·방법 서술 금지. 근거 질의 가드는 그대로.
- 통지 경고는 사용자가 직접 결정하므로 더 눈에 띄게(신청 전 확인 체크).

## 8. 내부 LLM (2026-10-31 오너 승인) — GPU 서버 자체 호스팅, 외부 LLM 런타임 금지
- 모델: `Qwen/Qwen3-8B-AWQ`(Apache-2.0, 공식 AWQ, thinking 끔). 대안 `kakaocorp/kanana-1.5-8b-instruct-2505`(Apache-2.0)로 10문항 A/B 후 확정. `[실제: Kanana 는 배포·A/B 하지 않았다(레드팀 요약). Qwen3-8B-AWQ 로 확정]` EXAONE 은 라이선스(other) 때문에 제외.
- 서빙: GPU 서버 vLLM OpenAI 호환 서버 `127.0.0.1:8001`(외부 바인드 금지), `gpu/` FastAPI 가 프록시. `--gpu-memory-utilization` 은 다른 모델(bge-m3·reranker·GLiNER)과 함께 24GB 안에 들어가게 조정(실측).
- 역할 3가지만 (모두 `gpu/` 엔드포인트, 운영 API 가 프록시):
  1. `POST /llm/intake` `{text}` → `{victim_type|null, flags:[id...], confidence, unclear:[질문]}` — JSON 스키마 강제(guided decoding), flag id 는 knowledge.json 목록만. 사용자가 체크박스로 확인·수정한 뒤에만 진단. **어떤 제도를 권할지는 규칙 엔진**.
  2. `POST /llm/answer` `{question, item_ids:[...]}` → 해당 지식표 행과 조문 원문만 컨텍스트로 쉬운 말 답 + 인용(item id·조문). 컨텍스트에 없으면 "확인되지 않음 · 1366/132 문의". 결과에 'AI 생성' 표시.
  3. `POST /llm/rewrite` `{text, spans}` → 위치 단서를 안전 문구로 바꾼 제안 문장(원문 의미 보존, 새 사실 추가 금지).
- 가드 `[대체됨 → §11.1·§11.5]`: 입력은 기존 `/intent`, 출력도 `/intent`(locate) + 규칙 필터로 한 번 더 검사해 차단. 사용자 텍스트 안 지시문 무시(프롬프트 주입 방어: 시스템 지시에 명시, 사용자 텍스트는 구분자로 감쌈).
- 로그: 본문·프롬프트·생성문 저장 금지(vLLM `--disable-log-requests`). `[실제: vLLM 0.32 플래그 `--no-enable-log-requests --no-enable-log-outputs --disable-uvicorn-access-log`, `gpu/llm_start.sh`]`
- 실패 시: 체크박스 방식으로 그대로 동작(외부 LLM 대체 금지).
- 평가: intake 는 합성 자유서술 20개(정답 flags) → flag F1·피해유형 정확도. answer 는 10문항 근거 이탈 여부(사람 판정 표본). rewrite 는 단서 잔존율.

## 9. 가드·노출 통제 보강 (2026-10-31 리뷰 반영)
- `[대체됨 → §11.1]` `/api/search` 의도 판정은 GPU `/intent` · 로컬 정규식 · 규칙 가드(`app/guard.py`, GPU 쪽 사본 `gpu/guard.py`)를 **항상 OR** 로 결합한다(fail-closed). 타인 지시어 + 소재·기록 명사 + 탐색 표현이 함께 나오면 보호 단어가 섞여도 거절.
- GPU `/intent` 는 시드별 최대 유사도(max-sim)로 비교, 임계값은 별도 검증 세트 `data/eval/guard_holdout.json`(평가 40개와 분리)으로 정한다(`INTENT_THRESH`, 기본 0.02).
- `data/laws/articles.jsonl` 각 조문에 `exposable`(bool)·`excerpt`(보호 조항 발췌 또는 null). 검색 말뭉치는 `exposable: true` 만, `excerpt` 가 있으면 그 문장만 돌려준다. 열람·교부·발급 절차 조문은 `tools/mark_exposable.py` 의 거부 목록으로 제외.
- 지식표 선택 키 `conditional_notes: [{"if_any": [flag], "note": "..."}]` → 진단 결과 checklist 항목의 `note`.
- 잔여 위험(residual) 응답에는 `exposure`·`limits` 를 싣지 않고 `support`(1366·132·112 연결)를 붙인다.


## 10. 2026-10-31 2차 리뷰 반영
- `[대체됨 → §11.1: 세 엔드포인트가 같은 판정을 쓰는 원칙은 유지, 판정은 2단 등급으로]` 입력 가드는 세 엔드포인트(/api/search·/api/intake·/api/answer)가 같은 판정을 쓴다: 규칙 OR 임베딩 > 0.02.
  예외는 `guard.self_protect_allow`(본인 우편물·휴대폰 명의·머물 곳, 소재 명사 없는 제도 질문·내 신청의 통지 질문)이고
  점수 0.15 이하일 때만 임베딩 판정을 푼다. 규칙에 피신청인·피의자·행위자·가해자 측, 행방·소재·머무는 곳, 수소문 추가.
- 생성문(답) 가드: 규칙 + 생성문 전용 `answer_locate_intent`(소재 명사 + '확인하려면/알 수 있습니다') OR 임베딩 > 0.15.
  실측상 임베딩은 정상 답(0.1302)과 주입 답(0.1133)을 가르지 못해 출력 임계값을 낮추지 않았다.
- 통지 질문은 생성하지 않고 통지 경고 원문만(없으면 '확인되지 않음 · 1366/132'). 근거 없는 통지 부정 답은 버린다.
  A6←A1, A9←A8 통지 경고를 물려받는다(`app/humanize.py`). 효과 시점 질문도 신청 시점·처리 칸 원문만.
- 질문이 근거 행과 다른 제도를 이름으로 대면 '확인되지 않음'(`gpu/llm.py topic_mismatch`).
- 사용자 문구의 내부 id 는 적재 시 제도 이름으로 바꾸고, 답의 인용은 '제도 이름 · 법령 조문'으로 보인다.
- rewrite: 입력 1500자 상한, 시간 초과는 504(재시도 없음, 전역 기능 표시 유지), 접속당 1건·전체 2건.
  원문에 도우미 지시문이 있으면 규칙 치환 결과를 쓴다. 정기 일정(요일·시각·등하교)은 '[일정 비공개]'.
- 조문 노출: 주민등록법 시행령 제47조의2(①만)·시행규칙 제13조의2(①만)·민사소송법 제163조(①~③) 발췌.
- 화면: '지금 먼저' 최대 5건 + 급한 잔여 위험을 위에, 나머지는 '더 보기'. 잔여 위험은 §7 문구만.
  자동 선택은 항목별 '빼기'와 '고른 항목을 확인했습니다' 체크 뒤에만 제출. 도움 줄에 112.

## 11. 실제 구현 (as-built, 2026-10-31 18시 기준)
배포본: 운영 서버 hk-ops 의 `app/`·GPU 서버 hk-gpu 의 `gpu/` 파일 `sha256sum` 이 저장소 HEAD(가드는 `2418ad9`)와 같고, 두 프로세스는 파일 갱신(17:56:52) 뒤 17:57 에 다시 떴다(`EVIDENCE.md` §6). 아래 값은 코드에서 옮겼다.

### 11.1 2단 입력 가드 (`app/guard.py`, `gpu/guard.py` 는 같은 파일)
- 등급 4개: `self_protect`(내 기록·내 안전) · `ambiguous` · `third_party_locate`(분명한 타인 소재 탐색) · `rephrase`(초성 줄임만으로 된 글).
- **거절은 `third_party_locate` 만.** 거절 응답 `{"refused": true, "tier": "third_party_locate", "message", "alternatives": [{id, label, measure}]}`.
  대안은 질문 낱말 → 보호 제도 표(`PROTECTIVE_MAP`, 13묶음)에서 2~3개, 잔여 위험(`status: none`) 행은 빼고 모자라면 기본 세트(A1·J1·P1)로 채운다. 문구에 1366·132·112 연결.
- `ambiguous` 는 거절하지 않고 결과에 `protective: {message, alternatives}` 를 붙여 진행한다. `rephrase` 는 다시 써 달라는 안내만(`/api/search` 는 결과 0건).
- 판정 순서(`_tier_one`): SPECIAL(문맥과 무관하게 거절하는 표현 묶음: 몰래 위치 확인·미행·열람 제한 우회 등) → 타인 소유 소재 명사 + 탐색 → 영어 표현 → 사람 찾기 표현 → 본인 보호 맥락 판정 → (보호 맥락이 없을 때) 규칙·구문·임베딩.
- 임베딩(GPU `/intent`, bge-m3 시드 max-sim 점수)은 **신호 하나**다: 점수 > `EMB_REFUSE` 0.16 이고 타인 지시어가 있고 본인 보호 맥락이 없을 때만 거절 근거, 점수 > `EMB_AMBIG` 0.02 면 `ambiguous`. 0.16 은 `data/eval/guard_holdout2.json` 으로만 정했다. `/intent` 호출이 실패하면(10초) 규칙만으로 판정한다.
- **정규화**(`normalize`·`decoded_variants`·`strip_variant`): NFKC, 자모 조합, 보이지 않는 문자 제거, 소리 나는 대로 쓴 표기 일부 치환, base64·URL(%xx)·`\uXXXX` 디코딩 변형, 공백·구두점을 지운 변형. 디코딩 변형 중 하나라도 `third_party_locate` 면 거절. 공백 제거 변형은 일부러 흩어 쓴 글일 때만 완전 판정.
- 적용 위치: `/api/search`·`/api/intake`·`/api/answer`(앱), `/llm/answer`(GPU, `input_flagged`, 이중). `/api/rewrite`·`/api/doccheck` 는 사용자 본인 서류라 입력 가드를 두지 않고 출력만 검사한다(§11.6).
- 생성 답이 출력 가드에 걸려 거절될 때도 대안을 붙인다(맨 거절 없음).

### 11.2 intake 강화 (`app/llm.py intake`, `app/guard.py`, `gpu/llm.py prepare_intake·clean_intake`)
- 보내기 전: 채팅 템플릿 특수 토큰·역할 머리말(`<|im_start|>`, `</think>` 류) 제거 → 도우미 지시문이 든 문장(`flags 에 … 넣어` 류) 제거. 남는 글이 없으면 생성하지 않는다. 응답에 `instructions_ignored` 표시.
- 받은 뒤: flag id 는 지식표 목록만, 그리고 **사용자 글에 근거 낱말이 있는 flag 만** 남긴다(`FLAG_EVIDENCE`, 표에 없는 flag 는 버림 = fail-closed). 피해 유형도 근거 낱말(`VICTIM_EVIDENCE`)이 있어야 남는다.
- 확인 질문(`unclear`, 최대 3개): 타인 소재를 캐는 질문은 버린다. 위치·이름·연락처를 묻는 질문은 같은 갈래의 **범주 질문**으로 바꾼다(예: '자녀가 학교에 다니나요?'; 그 flag 를 이미 골랐으면 뺀다). 사용자 글 되풀이, 이미 물은 질문(이력의 도우미 턴), 서로 겹치는 질문은 뺀다. GPU 쪽도 생성 질문에 `keyword_locate`·공통 출력 가드(§11.5)를 건다.
- intake 결과는 체크박스 **미리 선택**에만 쓰인다. 화면은 항목별 '빼기'와 '고른 항목을 확인했습니다' 체크 뒤에만 진단을 낸다. 어떤 제도를 권할지는 규칙 엔진(`app/engine.py`).

### 11.3 다중 턴 이력 (`app/main.py _WithHistory`)
- 브라우저가 메모리에만 들고 요청마다 `history: [{role: user|assistant, text}]` 로 보낸다. 서버는 무상태(저장·로깅 없음). 새로고침·빠른 나가기로 사라진다.
- 상한: 최근 **6턴**, 턴당 **500자**, 합계 **3000자**(넘으면 422, 입력을 되비추지 않는다).
- 입력 가드는 이번 말 + 최근 사용자 턴을 이어 붙인 글도 판정한다(`assess_turns`, 질문을 쪼개 보내는 우회 대비). 단, 이번 말이 본인 보호면 SPECIAL 이 아닌 한 뒤집지 않고, 낱말 묶음 규칙으로만 걸렸으면 이번 말에 탐색 표현이 있을 때만 거절한다(`2418ad9`).
- intake 는 최근 사용자 턴과 이번 말을 이어 붙인 뒤 끝 3000자를 쓴다. answer 생성은 이번 질문만 쓴다.

### 11.4 근거 답변: 생성 → 검증 → 재생성 → 발췌 (`gpu/verify.py run_answer`, `app/llm.py answer`)
- 생성 전 분기(생성하지 않음): 통지 질문 → 지식표 `notify_warning` 원문(없으면 '확인되지 않음 · 1366/132'). 효과 시점 질문 → `timing`·`where` 칸 원문. 질문이 근거 행에 없는 제도 이름을 대면 `not_found`(`topic_mismatch`).
- 컨텍스트: 고른 지식표 행(최대 8개, 칸 400자) + `exposable` 조문(발췌 우선, 행당 2개·600자, 개정 부기 제거). `exposure` 는 보내지 않고 잔여 위험 행은 `limits` 도 뺀다. 전체 14KB 상한.
- 검증: 문장 분리 → 규칙(숫자·날짜·기간·조문 번호는 근거에 있어야, 기관·제도 이름은 근거 행에 있어야, 통지·시점 단정은 통지 경고·신청 시점 칸과 맞아야) → 재순위(bge-reranker-v2-m3)로 문장별 근거 조각 3개(점수 < 0.01 이면 근거 없음) → Qwen 검증기(온도 0, JSON 스키마)로 문장별 뒷받침 판정. 1366·132·문의 안내 문장은 검증 대상에서 뺀다.
- 실패하면 피드백을 붙여 온도를 낮춰 **1회 재생성** → 재검증. 남은 시간이 4초 미만이거나 다시 실패하면 생성 없이 **발췌**(머리말 '관련 내용은 다음과 같습니다.' + 관련 높은 행 2개의 쉬운 설명·막는 제도·신청처 칸). 전체 예산 **12초**(`ANSWER_BUDGET_S`).
- `mode`: `generated` | `regenerated` | `extractive` | `not_found` | `refused`. 화면은 `extractive` 에 'AI 생성' 배지를 붙이지 않는다.
- 앱 쪽 후처리: 인용 id 는 컨텍스트 안의 것만, `grounded` 아니면 `not_found`. 생성문(발췌 제외)에 출력 가드(`answer_locate_intent` OR 임베딩 > `LLM_OUT_THRESH` 0.15) → 걸리면 대안 붙은 거절. 인용 행에 근거 없는 '상대에게 알려지지 않아요' 류 답은 버림. 내부 id(`[A1]`)는 제도 이름으로 바꾼다.

### 11.5 공통 출력 가드 (`gpu/verify.py OutputGuard.issues`)
answer·intake 확인 질문·rewrite 에 모두 쓴다. 걸리면 answer 는 발췌, 확인 질문은 버림, rewrite 는 규칙 치환.
1. 시스템 지시문 20-gram 일치(프롬프트 유출)
2. 노출 금지 조문 12-gram 일치 — 원문 대신 해시(`gpu/excluded_ngrams.json`, 배포본 `/health` 기준 24,995개)
3. 과잉 약속·단정 어휘('반드시'(확인·문의 권유 제외), '100%', '무조건' 등) — 근거 문구에 같은 표현이 있으면 허용
4. 목록 밖 인용 id
5. 반복 붕괴(같은 문장·조각 반복)

### 11.6 문장 다시 쓰기 정렬 (`gpu/server.py llm_rewrite`, `gpu/llm.py finalize_rewrite`)
- 단서 = 앱이 넘긴 span + 서버 정규식(도로명 주소·동호수·차량번호·전화·이메일·주민번호) + 정기 일정(요일·시각·등하교 → `[일정 비공개]`) + 모델이 찾은 clues.
- 모델 제안을 받아 남은 단서는 강제로 가린다. 다음이면 **규칙 치환**(모델 출력 버림): 원문에 없던 숫자, 원문에 도우미 지시문, 새 전화·이름·장소 이름, 원문 문장과 정렬되지 않는 문장(가림 표시를 같게 맞춘 뒤 `SequenceMatcher` 비율 < 0.6, `REWRITE_ALIGN_MIN`), 공통 출력 가드 적중, 모델 JSON 잘림(503 대신, `fc84eb4`).
- 결과에 남은 정규식 단서는 한 번 더 가린다. 입력보다 탐색 점수가 0.05 넘게 오르고 0.15 를 넘거나 새 규칙 적중이 생기면 거절.
- 응답 `method`: `llm` | `rule`(화면 배지). 앱은 원래 단서 문자열이 남았는지 `remaining` 으로 알려 준다.

### 11.7 LLM 경로 정책
- **런타임 LLM 은 GPU 서버의 vLLM Qwen3-8B-AWQ(127.0.0.1:8001) 하나뿐이다.** `gpu/llm.py` 의 `LLM_URL` 기본값이 이것이고, `app/`·`gpu/` 코드에는 다른 LLM 주소가 없다(`grep -rn "18090\|GLM" app gpu` 0건).
- 실패하면 대체 LLM 으로 넘기지 않는다: 503(연결 실패면 기능 표시를 30초 내림) 또는 504 → 화면은 체크박스 방식으로 계속.
- GLM-5.3-Flash(제3자 서버) 경로는 **합성 데이터 시뮬레이터·레드팀·시연용으로만** 있다: 두 서버의 `127.0.0.1:18090` 은 개발 기기의 중계로 이어지는 역방향 터널 끝점이고, 이를 부르는 코드는 `tools/sim/glm.py` 뿐이다. 보내는 것은 합성 카드와 서비스 응답뿐이며 시스템 프롬프트·소스는 보내지 않는다. `LLM_URL` 을 이 주소로 바꾸는 운영은 금지.
- **빌드·개발 때 쓴 외부 모델 기록**(출처: 개발 기록(2026-10-31 빌드 보고), 저장소 파일에는 모델 이름이 없다): 지식표 쉬운 설명(`plain`) 초안 A1–J19 42건은 DeepSeek-V4.1-Flash(MCP 경유, 공개 조문만 보냄)가, J20–P9 41건은 그 게이트웨이가 HTTP 400 을 돌려줘 Claude 가 썼다. 이후 검토에서 Claude 가 12건을 다시 쓰고 J28 을 더했다(그래서 조사 문서의 83경로보다 1행 많은 84행). 84건 모두 `plain_reviewed=false`. 코드는 1차 일부를 DeepSeek 가 초안으로 쓰고, 이후 회차는 Claude 가 썼다. 사람(법률 지식 보유자) 검수는 아직 없다.

### 11.8 상한·시간 제한
| 항목 | 값 | 위치 |
|---|---|---|
| `/api/search` q | 500자 | `app/main.py` |
| `/api/intake` text | 2000자 (+ 이력 §11.3) | `app/main.py` |
| `/api/answer` question / item_ids | 500자 / 1~8개 (`^[A-Z][0-9]{1,3}$`) | `app/main.py` |
| `/api/search_answer` q | 500자 (+ 이력 §11.3), `strategy` 는 `rows`·`articles` 밖이면 422 | `app/main.py` (§11.11) |
| `/api/doccheck` text | 20000자 | `app/main.py` |
| `/api/rewrite` text / spans | 1500자 / 60개 | `app/main.py` (1500자 = 실측 38.9초, 60초 제한 안) |
| `/api/rewrite` 동시 처리 | 접속 주소당 1건, 전체 2건, 넘으면 429 | `app/main.py` |
| `/api/diagnose` flags | 60개, 알 수 없는 flag 422, 다른 필드 422 | `app/main.py` |
| 앱 → GPU 시간 제한 | `/intent`·`/ner` 10초, intake 60초, answer·rewrite 90초, `/health` 3초(30초 캐시) | `app/main.py`, `app/llm.py` |
| 재시도 | GPU 가 503 을 돌려준 경우만 1회. 504(시간 초과)는 재시도 없음 | `app/llm.py _post` |
| GPU 본문 상한 | 20KB(413) | `gpu/server.py` |
| GPU 인증 | `GET /health` 밖 모든 경로에 `X-Internal-Token`(없거나 틀리면 401, 토큰 파일 없으면 전부 401) | `gpu/server.py`, `app/main.py` (§11.10) |
| GPU `/ner` 조각 | 700자, 겹침 100자 | `gpu/server.py` (§11.10) |
| GPU → vLLM | 60초(`LLM_TIMEOUT`), 답변 전체 12초 예산, rewrite `max_tokens` = min(4096, 2×길이+512) | `gpu/llm.py`, `gpu/verify.py` |
| vLLM | `--max-model-len 8192`, `--gpu-memory-utilization 0.6` | `gpu/llm_start.sh` |
| 응답 머리글 | CSP `default-src 'self'`, `Cache-Control: no-store`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `nosniff` | `app/main.py` |

### 11.10 GPU 토큰·위치 단위·NER 조각·메모리 가드 (2026-11-01, `642e5e1`)
배포 기록 `docs/evidence/deploy-2026-11-01.md`, 시험 판정 `docs/TEST_RESULTS.md` 「2026-11-01 수정·배포·재측정」.
- **GPU API 토큰 인증**: hk-gpu `0.0.0.0:8000` 은 SSL-VPN 클라이언트에서도 닿는다. NCP 콘솔(ACG)은 오너가 쓸 수 없으므로 애플리케이션 단계에서 막는다.
  - `GET /health` 를 뺀 모든 경로에 `X-Internal-Token` 헤더가 필요하다. 토큰은 `GPU_TOKEN_FILE`(기본 `~/.config/sumeunjip/gpu_token`, 폴더 700·파일 600)에서 요청마다 읽고 `hmac.compare_digest` 로 비교한다. 파일이 없거나 비면 전부 401(fail-closed). 401 본문은 `{"detail":"unauthorized"}` 뿐이고 헤더 값은 로그하지 않는다.
  - `/health` 는 참·거짓만 돌려준다: `ok`, `models{embedder,reranker,gliner}`, `cuda`, `llm`, `auth`(토큰 파일 있음), `ngrams`(노출 금지 n-gram 표 실림). 장치 이름·n-gram 개수·예산 값은 더 이상 내보내지 않는다.
  - 앱은 GPU 를 부르는 `httpx.AsyncClient` 의 기본 헤더에 토큰을 싣는다(`app/main.py gpu_auth_headers`). 토큰 파일이 없으면 GPU 가 401 → 각 기능은 기존 실패 경로대로 체크박스 방식으로 동작한다. 개발용 `tools/mock_gpu.py` 는 토큰을 검사하지 않는다.
  - 실측: 토큰 없음·틀린 토큰 401(hk-ops·VPN 노트북), 올바른 토큰 200, 로그 3개의 토큰 출현 0건.
- **위치 단위 `offset_unit`** (`app/offsets.py`): 브라우저는 UTF-16 코드 단위, 파이썬은 코드 포인트로 센다. 앱은 `/api/rewrite` 로 받은 span 을 코드 포인트로 바꿔 GPU 로 넘기고, `/api/doccheck` 가 돌려주는 span 은 UTF-16 으로 바꾼다. 두 응답(거절·사용 불가 응답 포함)에 `offset_unit: "utf16"` 을 붙인다. 이모지·보충 평면 문자 뒤 단서가 덜 가려지던 결함(P0-04)을 고친다.
- **NER 조각 분할** (`gpu/server.py chunk_text`): GLiNER 는 앞쪽 약 700자만 본다. `/ner` 는 글을 700자 조각(이웃과 100자 겹침, 가능하면 공백·줄바꿈에서 자름)으로 나눠 돌리고, 위치를 원문 기준으로 되돌려 `merge_spans` 로 합친다. 정규식 단서는 전처럼 전체 글에서 찾는다. 배포본 실측: 700자 뒤 합성 단서 0/8 → 8/8(P0-05).
- **CUDA OOM → 503**: `RuntimeError` 처리기가 CUDA 메모리 부족(`torch.cuda.OutOfMemoryError` 또는 'out of memory')이면 캐시를 비우고 `503 {"detail":"gpu busy"}` 를 돌려준다(앱의 LLM 호출은 503 이면 1회 재시도, `/api/doccheck` 는 `available:false`). 다른 `RuntimeError` 는 그대로 500. 본문 4KB(`EMPTY_CACHE_BYTES`) 넘는 요청 뒤에는 `torch.cuda.empty_cache()`. `gpu/start.sh` 가 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 로 기동한다. 재기동 뒤 GPU 서버 프로세스 5,686~5,790 MiB(OOM 직후 8,566).
- 남은 것: `tools/eval*.py` 는 GPU 를 직접 부를 때 토큰을 보내지 않는다(운영 GPU 에서 401).

### 11.11 검색 조문 종합 답변 `POST /api/search_answer` (2026-11-01, 브랜치 `feature/search-answer`)
검색으로 고른 조문 5개만 근거로 질문에 답한다. 화면(`web/`) 변경은 없다 — 아래는 프런트 담당을 위한 계약이다.
구현 `app/search_answer.py`, 엔드포인트 `app/main.py`, 시험 `tests/test_search_answer.py`(모의 GPU).

- **요청**: `POST /api/search_answer?strategy=rows|articles` (생략 시 `rows`), 본문 `{"q": "...", "history"?: [{role, text}]}`.
  `q` 1~500자, 이력은 `/api/answer` 와 같은 상한(§11.3: 6턴·턴당 500자·합 3000자, `role` 은 user·assistant). 모르는 필드·잘못된 `strategy` 는 422(입력을 되비추지 않음). 접근 코드는 다른 API 와 같다.
- **흐름**: 입력 가드(`/api/search` 와 같은 2단 판정, 이력의 사용자 턴 포함) → 조문 검색 상위 5개(`/api/search` 와 같은 `Retriever.search`) → 종합 답변 → 출력 가드(`/api/answer` 와 같음: 생성문 규칙 `answer_locate_intent` OR 임베딩 > `LLM_OUT_THRESH`, 근거 없는 '알려지지 않아요' 는 '확인되지 않음' 으로 바꿈). 외부 LLM 호출 없음(GPU 내부 `/llm/answer` 만).
- **응답(정상)**:
  ```json
  {"refused": false, "tier": "neutral|ambiguous|self_protect",
   "strategy": "rows", "fallback": false,
   "results": [{"law","article","effective","text","url","score"}],
   "answer": "…", "mode": "generated|regenerated|extractive|not_found|unavailable",
   "grounded": true, "ai_generated": true,
   "citations": [{"law": "…", "article": "제29조", "url": "https://www.law.go.kr/…"}],
   "protective"?: {"message", "alternatives", "contacts"},
   "rows"?: ["A1"], "sources"?: [{"id", "label"}], "message"?: "…"}
  ```
  - `results` 는 `/api/search` 의 결과와 같은 모양(조문 5개 이하). `citations` 는 **반드시 `results` 안의 조문**만, 검색 순위 순. 인용이 검색 집합 밖이면 답하지 않고 `not_found`.
  - `fallback: true` 는 임베딩 검색이 안 돼 키워드(BM25) 검색으로 고른 결과다. 답은 그대로 내지만 화면에 '키워드 검색 결과' 표시를 권한다.
  - `ai_generated` 는 `/api/answer` 와 같은 뜻: 생성문이면 true, 발췌(`extractive`)·통지 질문 답은 false. 배지는 `mode` 로 고른다.
  - `mode: "not_found"` 면 `answer` 는 '제공된 자료에서는 확인되지 않습니다 … 1366·132' 안내, `citations: []`.
  - `mode: "unavailable"`: LLM 이 꺼져 있거나(`LLM_FEATURES=0`) GPU 생성이 실패·시간 초과. **503 대신 200** 으로 `answer: null`, `message`(안내 문구)와 검색 결과를 그대로 준다 → 화면은 검색 결과만 보여 주면 된다.
  - **시행 전 조문**: 인용 조문의 `effective`(YYYY-MM-DD)가 오늘(KST)보다 뒤면 답 끝에 고정 안내 '※ 시행 전 조문: 법령 조문(시행일 시행) — 아직 시행되지 않아 지금은 이 조문으로 신청할 수 없습니다.' 를 붙인다(두 방식, 생성·발췌 모두, 가드 통과 뒤). `articles` 방식은 그 조문의 `measure` 앞에 '[시행 전: … 지금은 적용되지 않음]' 을 붙여 모델에도 알리고, 앱 발췌의 조문 제목 줄에도 '(시행일 시행 예정, 시행 전)' 을 단다. 2026-11-01 품질 실측에서 스토킹 피해자보호명령(2027-04-22 시행)을 두 방식 모두 지금 신청할 수 있다고 답한 데 대한 수정.
  - `rows`·`sources` 는 `rows` 방식에서만: 근거로 쓴 지식표 행 id(내부용)와 제도 이름 표시(`/api/answer` 의 `sources` 와 같음). 화면에는 `citations` 를 쓰고 `rows` 는 보이지 않는다.
- **응답(거절·다시 쓰기)**: 타인 소재 탐색 → `{"refused": true, "tier": "third_party_locate", "message", "alternatives", "results": [], "fallback": false}`(검색·생성 안 함). 초성·줄임말 → `{"refused": false, "rephrase": true, "tier": "rephrase", "message", "results": []}`. 생성문이 출력 가드에 걸리면 `/api/answer` 와 같은 거절(`refused: true`, 보호 대안)이고 **검색 결과도 내보내지 않는다**(`results: []`).
- **두 방식**(같은 가드 뒤):
  - `rows`(기본): 검색 조문을 `basis` 로 인용하는 지식표 행을 역색인(시작 때 한 번, `(법령, 조 단위 조문)` → 행 id)으로 찾아 후보로 삼고, 아래 규칙으로 **최대 2개**만 기존 `llm.answer` 에 넘긴다(2026-11-01 1차 실측에서 행 5개를 넘기면 생성문이 다른 제도의 행을 섞었다). 조문 컨텍스트는 검색된 조문만 넘긴다. 통지·효과 시점 질문은 `/api/answer` 처럼 생성하지 않고 지식표 칸으로 답한다. 이어지는 행이 하나도 없으면 GPU `/llm/answer` 를 부르지 않고 `not_found`(실데이터: 노출 가능 조문 131건 모두 1개 이상 행에 이어진다).
    1. **제도·피해 유형 불일치 제외**: 질문이 이름을 댄 제도(`TOPIC_TERMS`, `gpu/llm.py` 의 `topic_mismatch` 목록과 같음 — 시험이 확인)나 따옴표로 댄 제도 이름('…'·"…"·「…」, 공백 무시·끝의 '제도' 제외)이 행의 `path`·`measure`·`plain` 에 없으면 그 행을 뺀다. GPU `topic_mismatch` 는 보낸 행 **전체**를 한 덩어리로 보므로 섞인 행을 못 거른다 → 앱이 행 단위로 거른다. 질문이 피해 유형(가정폭력·스토킹·성폭력·아동학대)을 대고 행 제목(`path`·`measure`)이 다른 유형만 대면 그 행도 뺀다.
    2. **관련도**: 행 글(`path`+`measure`+`plain`, 600자)을 기존 GPU `/rerank` 로 질문과 잰다(**gpu/ 변경 없음**, 요청당 1회 추가 호출, 실측 108~254ms). 행이 관련 있다 = 행 재순위 ≥ **0.03**(`ROW_RERANK_MIN`) 또는 (임베딩 검색일 때만) 그 행이 드는 검색 조문의 재순위 점수 ≥ **0.05**(`ARTICLE_RERANK_MIN`). `/rerank` 가 실패하면 질문 글자 bigram(어미류 제외) 중 행 글에도 있는 비율 ≥ **0.15**(`LEXICAL_MIN`)로 대신한다(분리력이 약한 대체 기준).
    3. **순서·개수**: 행 재순위 ≥ 0.03 인 행을 점수 순으로 먼저, 나머지는 검색 순위 순. 두 번째 행은 첫 행이 재순위로 뽑혔으면 그 점수의 **0.5배 이상**(`SECOND_ROW_RATIO`)이고 자신도 ≥ 0.03 일 때만 붙인다.
    4. **무관 답 차단**: 관련 있는 행이 하나도 없으면 GPU 생성 없이 `not_found`. 생성·발췌 결과가 관련도 기준을 넘는 행을 하나도 인용하지 않았으면 `not_found`(방어선).
    - 문턱 근거: round-1 개발 세트(`data/eval/search_answer_eval.json` 40문) 실측. 답 있는 30문은 모두 2번 기준을 넘고(행 재순위 최고값 최소 0.010·그때 조문 0.183), 답 없는·없는 제도 10문 중 7문은 행 재순위 최고 ≤ 0.016·조문 ≤ 0.021 로 걸린다. 개발 세트로 정한 값이라 별도 보류 세트로 다시 재야 한다.
    - 개발 세트 실측(2026-11-01, 로컬 앱 + 배포 GPU 터널, 같은 40문 2회): 답 있는 30문 중 26문 답함(나머지: 입력 가드 거절 2·`not_found` 2 — '이혼'·'응급조치' 제도 이름 필터), 인용에 정답 키 포함 26/26, 답 없는·없는 제도 `not_found` 9/10 → 따옴표 이름 규칙 추가 뒤 10/10. 정답률은 생성문 편차로 회차마다 23~25/26(남은 오류는 한 행 안의 칸을 섞는 생성 오류). 지연 평균 5.8초·p90 10.0초(이전 12.5·17.3초), `not_found` 는 GPU 생성 없이 평균 1.8초.
  - `articles`: 검색 조문 자체를 GPU `/llm/answer` 의 items 로 보낸다(**gpu/ 변경 없음**). 조문마다 합성 id `Z1`…`Z5`(GPU `ItemIn.id` 정규식 `^[A-Z]\d{1,3}$` 통과, 지식표 id 첫 글자 A·D·J·L·P·S 와 겹치지 않게 `Z`), `measure` = '법령 조문(조 제목)', `plain` = 조문 본문(개정 부기 제거, 800자). 돌아온 인용 id 는 조문으로 되짚고, 본문의 `[Z1]` 표시는 지운다. GPU 가 발췌(`extractive`)로 떨어지면 행 칸 이름('막는 제도' 등)이 붙은 GPU 발췌 대신 앱이 인용 조문 원문 발췌(최대 2개·400자)로 다시 만든다. 조문에는 통지 경고·신청 시점 칸이 없으므로 통지 질문은 '확인되지 않음 · 1366/132', 효과 시점 질문은 `not_found`(생성 안 함).
- **한계**: `articles` 방식은 GPU 쪽 시스템 지시문이 지식표 행 칸(막는 제도·쉬운 설명)을 전제로 쓰여 있어, 조문 본문이 '쉬운 설명' 칸 이름으로 들어간다. GPU `topic_mismatch`·검증기는 그대로 돈다(질문이 이름을 댄 제도가 조 제목·본문에 없으면 `not_found`). 이를 조문 전용 지시문으로 바꾸려면 gpu/ 변경·재기동이 필요해 하지 않았다. 배포본 실측 품질(정답률·지연)은 아직 없다(모의 GPU 시험만).

### 11.9 아직 남은 것 (2026-10-31 18시)
- 다턴 시뮬레이션 v3(배포 `fc84eb4`)의 정상 질문 거절 23건 중 7건은 `2418ad9` 로 해소(같은 23문장 재전송 실측, 커밋 메시지). 16건이 남았다는 것은 커밋 메시지뿐인 중간 값이다(결과 파일 없음). **현행 값은 보정 뒤 배포본(`cf8d149`+`6672867`)으로 v3 사용자 대화를 다시 돌린 18건/14대화**다(`data/eval/notice_calibration_summary.md`; 방법이 달라 16과 직접 비교하지 않는다). 남은 유형: 아이 학교·위치 추적 의심 물건·'같이 산 적 없다'는 부정 답.
- 보호 안내가 v3 사용자 카드 40장 중 38장(응답 96건)에 붙던 문제는 `cf8d149`(neutral 등급 추가, `needs_notice()` 한 곳에서 결정)로 고쳤다. 배포본 실측 1/40장·1/225건(`data/eval/notice_calibration_summary.md`).
- 지식표 84행의 쉬운 말 설명은 모두 사람 검수 전(`plain_reviewed: false` 84/84).
