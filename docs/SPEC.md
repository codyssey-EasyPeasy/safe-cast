# 숨은집 지킴이 구현 명세 (v1, 2026-10-31)

마감 2026-11-01 13:00 KST. 아키텍처: `architecture.md`. 근거: `../research/2026-10-31-정보조사.md`.

## 0. 공통 규칙
- Python 3.12, `uv` 사용. 런타임 의존성 최소화. 한국어 UI·메시지.
- **사례 정보 비저장·비로깅**: 요청 본문을 로그에 남기지 않는다(접근 로그는 시각·경로·상태코드만). DB 없음.
- 외부 LLM 호출은 `tools/build_*` 빌드 스크립트에서만. `app/`·`gpu/`·`web/` 런타임 코드는 외부 인터넷을 호출하지 않는다(GPU 서버 모델 최초 다운로드 제외).
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
- `POST /api/search` `{"q": "..."}` → 가드(의도 분류) 통과 시 GPU `/embed` + `/rerank` 로 법령 조문 상위 5개 `{law, article, effective, text, url, score}`. 거절 시 `{"refused": true, "message": "..."}`. GPU 불가 시 키워드(BM25 간이) 폴백, `"fallback": true`.
- `POST /api/doccheck` `{"text": "..."}` → GPU `/ner` 결과(직장·학교·생활권 단서 span)만 반환. 본문 로그 금지. GPU 불가 시 `{"available": false}`.
- `GET /api/health`.
- 설정: 환경변수 `GPU_URL`(기본 `http://10.1.2.33:8000`), `ACCESS_CODE`(상담원 기관 코드, 헤더 `X-Access-Code`; 미설정이면 데모 모드로 통과).

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
- 모델: `Qwen/Qwen3-8B-AWQ`(Apache-2.0, 공식 AWQ, thinking 끔). 대안 `kakaocorp/kanana-1.5-8b-instruct-2505`(Apache-2.0)로 10문항 A/B 후 확정. EXAONE 은 라이선스(other) 때문에 제외.
- 서빙: GPU 서버 vLLM OpenAI 호환 서버 `127.0.0.1:8001`(외부 바인드 금지), `gpu/` FastAPI 가 프록시. `--gpu-memory-utilization` 은 다른 모델(bge-m3·reranker·GLiNER)과 함께 24GB 안에 들어가게 조정(실측).
- 역할 3가지만 (모두 `gpu/` 엔드포인트, 운영 API 가 프록시):
  1. `POST /llm/intake` `{text}` → `{victim_type|null, flags:[id...], confidence, unclear:[질문]}` — JSON 스키마 강제(guided decoding), flag id 는 knowledge.json 목록만. 사용자가 체크박스로 확인·수정한 뒤에만 진단. **어떤 제도를 권할지는 규칙 엔진**.
  2. `POST /llm/answer` `{question, item_ids:[...]}` → 해당 지식표 행과 조문 원문만 컨텍스트로 쉬운 말 답 + 인용(item id·조문). 컨텍스트에 없으면 "확인되지 않음 · 1366/132 문의". 결과에 'AI 생성' 표시.
  3. `POST /llm/rewrite` `{text, spans}` → 위치 단서를 안전 문구로 바꾼 제안 문장(원문 의미 보존, 새 사실 추가 금지).
- 가드: 입력은 기존 `/intent`, 출력도 `/intent`(locate) + 규칙 필터로 한 번 더 검사해 차단. 사용자 텍스트 안 지시문 무시(프롬프트 주입 방어: 시스템 지시에 명시, 사용자 텍스트는 구분자로 감쌈).
- 로그: 본문·프롬프트·생성문 저장 금지(vLLM `--disable-log-requests`).
- 실패 시: 체크박스 방식으로 그대로 동작(외부 LLM 대체 금지).
- 평가: intake 는 합성 자유서술 20개(정답 flags) → flag F1·피해유형 정확도. answer 는 10문항 근거 이탈 여부(사람 판정 표본). rewrite 는 단서 잔존율.

## 9. 가드·노출 통제 보강 (2026-10-31 리뷰 반영)
- `/api/search` 의도 판정은 GPU `/intent` · 로컬 정규식 · 규칙 가드(`app/guard.py`, GPU 쪽 사본 `gpu/guard.py`)를 **항상 OR** 로 결합한다(fail-closed). 타인 지시어 + 소재·기록 명사 + 탐색 표현이 함께 나오면 보호 단어가 섞여도 거절.
- GPU `/intent` 는 시드별 최대 유사도(max-sim)로 비교, 임계값은 별도 검증 세트 `data/eval/guard_holdout.json`(평가 40개와 분리)으로 정한다(`INTENT_THRESH`, 기본 0.02).
- `data/laws/articles.jsonl` 각 조문에 `exposable`(bool)·`excerpt`(보호 조항 발췌 또는 null). 검색 말뭉치는 `exposable: true` 만, `excerpt` 가 있으면 그 문장만 돌려준다. 열람·교부·발급 절차 조문은 `tools/mark_exposable.py` 의 거부 목록으로 제외.
- 지식표 선택 키 `conditional_notes: [{"if_any": [flag], "note": "..."}]` → 진단 결과 checklist 항목의 `note`.
- 잔여 위험(residual) 응답에는 `exposure`·`limits` 를 싣지 않고 `support`(1366·132·112 연결)를 붙인다.

