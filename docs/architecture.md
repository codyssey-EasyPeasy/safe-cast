# 보안 예보 아키텍처 (실제 구현, 2026-10-31 18시)

근거 조사: `../research/2026-10-31-정보조사.md`. 명세: `SPEC.md`(§11 이 실제 동작의 정본). 실증 자료: `EVIDENCE.md`. 발표 자료: `../pt/index.html`.
처음 초안(오전)은 git 이력 `d677ea0` 에 있다. 이 문서는 배포본을 그대로 적는다. 배포본 앱 12개·GPU 11개 파일 해시는 커밋 `6672867` 과 같다(2026-10-31 21:0x 재대조, `evidence/infra-2026-10-31-night.md` 6절).

## 원칙
1. 사용자는 피해자 본인과 가족·지인이다(2026-10-31 오너 결정으로 상담원 경유 제거). 인증 없이 쓰되 안전한 기기 안내·빠른 나가기·기록 비저장으로 보완하고, 결과마다 1366·132(급하면 112)를 붙인다.
2. 출력은 **막는 제도만**. 사람을 찾는 방법·조회 요건·순서는 다루지 않는다. 열람·교부 절차 조문은 검색 말뭉치에서 뺀다.
3. **어떤 제도를 권할지는 규칙 엔진이 정한다.** AI 는 검색·재순위·서류 단서 탐지·의도 신호·문장 보조(상황 정리, 근거 답, 다시 쓰기)에만 쓴다.
4. 사례 정보(상황 체크값·자유서술·서류 원문·대화 이력·결과)는 서버에 저장하지 않고 로그에 남기지 않는다.
5. 서비스 실행 중 외부 LLM 호출 0. 내부 LLM 은 GPU 서버에서 직접 띄운 Qwen3-8B-AWQ 하나뿐이다.

## 배치도 (텍스트)
```
 [사용자 브라우저]  web/index.html + static/app.js · detect.js
   - 피해 유형·상황 체크, 결과·인쇄, 서류 점검 1단계(브라우저 안 정규식)
   - 대화 이력은 메모리 변수만(localStorage·쿠키 없음), 빠른 나가기
        │  HTTPS 없음(시연은 ssh 터널 -L 18080:127.0.0.1:8080)
        ▼
 ┌──────────────── hk-ops (운영 서버, 10.1.1.33, 2 vCPU · 7.8GiB)*────────────────┐
 │ 127.0.0.1:8080  FastAPI app/  (uvicorn, 접근 로그 끔)                           │
 │   engine.py   규칙 진단 (지식표 84행 × 피해 유형 4 × 상황 flag 30)              │
 │   guard.py    2단 입력 가드·정규화·intake 강화          ← 1차 가드              │
 │   retrieval.py 조문 검색(임베딩+재순위, 실패 시 BM25), 출력 의도 검사          │
 │   llm.py      GPU /llm/* 프록시, 통지·시점 질문 분기, 출력 가드, 인용 정리      │
 │   humanize.py 내부 id → 제도 이름                                               │
 │ 127.0.0.1:18090  GLM 중계 터널 끝점 (시뮬레이터·레드팀 도구 전용, 앱은 안 씀)   │
 └────────────────────────────────┬───────────────────────────────────────────────┘
                                  │ 내부망 http (10.1.2.33:8000) + X-Internal-Token
                                  ▼
 ┌──────────────── hk-gpu (추론 서버, 10.1.2.33, 4 vCPU · 47GiB · NVIDIA L4 23GB) ─┐
 │ 0.0.0.0:8000  FastAPI gpu/server.py  (/health 밖은 토큰 필요)                  │
 │   /embed  bge-m3            /rerank  bge-reranker-v2-m3                        │
 │   /ner    GLiNER multi-v2.1 (+정규식)   /intent  시드 max-sim 점수             │
 │   /llm/intake · /llm/answer · /llm/rewrite                                     │
 │   guard.py (앱과 같은 파일, 2차 입력 가드)  verify.py (공통 출력 가드·답 검증)  │
 │ 127.0.0.1:8001  vLLM 0.32  Qwen3-8B-AWQ (thinking 끔, 요청·생성 로그 끔)       │
 │ 127.0.0.1:18090 GLM 중계 터널 끝점 (위와 같음, 런타임 미사용)                  │
 └────────────────────────────────────────────────────────────────────────────────┘

 [빌드 시점, 경계 밖]  국가법령정보센터 현행 조문 → data/laws/articles.jsonl(174건)
                       → exposable·발췌 표시(tools/mark_exposable.py) → 지식표 data/knowledge.json(84행)
```

## 구성 요소
| 구성 요소 | 위치·포트 | 역할 |
|---|---|---|
| 웹 클라이언트 | 브라우저 (`web/`) | 바닐라 JS, 외부 CDN 없음. 서류 점검 1단계(`detect.js`), 통지 경고 확인 체크, intake 미리 선택 확인 뒤 제출, 빠른 나가기 |
| 운영 API | hk-ops `127.0.0.1:8080` (`app/`) | `/api/diagnose`·`/api/search`·`/api/doccheck`·`/api/intake`·`/api/answer`·`/api/rewrite`·`/api/features`·`/api/health` |
| 진단 엔진 | `app/engine.py` | 지식표 행의 `victim_types`·`triggers` 평가, 급한 순(urgency → 영역 → id) 정렬, pending·잔여 위험·'이 유형엔 제도 없음' 분리 |
| 지식 데이터 | `data/knowledge.json`, `data/laws/articles.jsonl` (읽기 전용) | 84행(시행 67·시행 예정 1·막을 제도 없음 16), 조문 174건(노출 가능 131, 발췌 7) |
| 추론 API | hk-gpu `0.0.0.0:8000` (`gpu/`) | 임베딩·재순위·NER(700자 조각)·의도 점수·LLM 3기능 프록시와 검증. `GET /health` 밖 모든 경로에 공유 토큰 `X-Internal-Token` 필요(`SPEC.md` §11.10) |
| 내부 LLM | hk-gpu `127.0.0.1:8001` | vLLM OpenAI 호환, `qwen3-8b`, `--max-model-len 8192`, JSON 스키마 강제 디코딩 |

\* 서버 사양은 2026-10-31 20:54 `lscpu`·`nproc`·`free -h` 실측(hk-ops Xeon Gold 5220, hk-gpu Xeon Silver 4416+, 둘 다 Ubuntu 24.04.1 LTS). 원본 `evidence/infra-2026-10-31-night.md` 1절.

## 모델과 메모리 (hk-gpu, `nvidia-smi`, 2026-10-31 20:5x — 모델 서버 재기동 뒤)
| 프로세스 | 모델 | GPU 메모리 |
|---|---|---|
| vLLM (pid 509919) | Qwen/Qwen3-8B-AWQ (Apache-2.0) | 13,948 MiB (`--gpu-memory-utilization 0.6`) |
| 추론 API (pid 533224) | BAAI/bge-m3, BAAI/bge-reranker-v2-m3, urchade/gliner_multi-v2.1 | 6,518 MiB |
| 합계 | | 20,480 / 23,034 MiB (18:1x 재기동 전에는 20,912 — 추론 API 프로세스 메모리가 시점마다 다르다) |
| 2026-11-01 재기동 뒤(`642e5e1`, 메모리 가드) | 추론 API pid 546407 | 5,686~5,790 MiB, 합계 19,648~19,752 / 23,034 MiB. 동시 8 부하 중 최대 20,476 MiB(여유 최소 2,558) |

## 데이터 흐름
1. **진단**: 브라우저 → `/api/diagnose {victim_type, flags}` → 규칙 엔진 → 체크리스트·잔여 위험·해당 없음. LLM·GPU 를 거치지 않는다.
2. **상황 정리(intake)**: 자유서술 + 이력 → 앱 2단 가드(GPU `/intent` 점수는 신호) → 특수 토큰·지시문 문장 제거 → GPU `/llm/intake`(JSON 스키마) → flag 근거 낱말 검사·확인 질문 거르기 → 브라우저에서 미리 선택, **사용자가 확인한 뒤에만** 진단.
3. **근거 답(answer)**: 질문 + 고른 항목 id → 앱 2단 가드 → (통지·시점 질문은 지식표 원문) → GPU 2차 가드 → 생성 → 검증(규칙·재순위·Qwen 검증기) → 1회 재생성 → 발췌 → 공통 출력 가드 → 앱 출력 가드·인용 정리 → 브라우저.
4. **근거 검색(search)**: 질의 → 2단 가드 → 노출 가능 조문만 임베딩+재순위(GPU 불가·임베딩 미완료면 BM25, `fallback: true`).
5. **서류 점검**: 1단계는 브라우저 안 정규식(서버로 안 보냄). 2단계는 사용자가 누를 때만 `/api/doccheck` → GPU `/ner` → 단서 위치(span)만 돌려준다.
6. **다시 쓰기(rewrite)**: 원문 + span → GPU 정규식·정기 일정 단서 추가 → Qwen 제안 → 정렬·새 사실·출력 가드 검사 → 실패면 규칙 치환.

## 신뢰 경계
| 경계 | 믿지 않는 쪽 | 통제 |
|---|---|---|
| 브라우저 → 앱 | 사용자 입력·이력 전부 | pydantic 상한(`extra=forbid`), 2단 가드, 422 에서 입력 되비추지 않음, 보안 머리글(CSP·no-store·DENY) |
| 앱 → GPU | 내부망 + SSL-VPN 사용자(포트가 VPN 에서 닿음) | 공유 토큰(`X-Internal-Token`, 상수 시간 비교, 토큰 파일 없으면 전부 401), 본문 20KB 상한, CUDA OOM 은 503, GPU 쪽에서 같은 가드 재검사(이중), span 위치 단위 변환(UTF-16 ↔ 코드 포인트, `offset_unit`) |
| 사용자 글 → LLM | 프롬프트 주입 | 특수 토큰·역할 머리말·지시문 문장 제거, 구분자로 감쌈, JSON 스키마 강제 |
| LLM → 사용자 | 생성문 전부 | 공통 출력 가드(프롬프트 20-gram·금지 조문 12-gram 해시·단정 어휘·인용 id·반복), 답 문장별 검증, 탐색 표현 출력 가드, rewrite 정렬 검사 |
| 저장소 → 서버 | — | 지식표·조문은 읽기 전용 파일. 배포본 해시를 `sha256sum` 으로 대조 |
| 개발 기기 중계 ↔ GLM(제3자) | 제3자 서버 | 시뮬레이터·레드팀 도구만 사용. 합성 카드·서비스 응답만 보냄 |

## 남기지 않는 것·보내지 않는 것
- **앱 로그**: 메서드·경로·상태코드 한 줄(`AccessLogMiddleware`). 본문·쿼리스트링·IP 없음. uvicorn 접근 로그 끔(`scripts/start_app.sh`).
- **GPU 로그**: uvicorn 접근 로그가 켜져 있다(`gpu/start.sh --access-log`). 줄 형식은 `내부 IP:포트 - "POST /경로" 상태`(배포본 `gpu.log` 8,752줄을 숫자→N 으로 바꿔 69개 모양 전수 집계, `evidence/infra.md` 6절). 본문은 남지 않는다. 앱 서버 내부 IP 만 찍힌다.
- **vLLM**: `--no-enable-log-requests --no-enable-log-outputs --disable-uvicorn-access-log`. `HF_HUB_OFFLINE=1`·`VLLM_NO_USAGE_STATS=1`.
- **코드**: `app/llm.py`·`gpu/verify.py` 는 로거를 쓰지 않는다. 검증 메타(`verify`)는 숫자만.
- **브라우저**: localStorage·sessionStorage·쿠키 없음. 이력은 메모리 변수, 새로고침·빠른 나가기로 사라짐.
- **DB 없음.** 지식표·조문 외에 서버가 쓰는 파일은 pid·로그뿐.
- **외부로 나가는 것**: 런타임 0(앱·GPU 코드에 외부 주소 없음). `gpu/start.sh` 는 `HF_HUB_OFFLINE=1`·`TRANSFORMERS_OFFLINE=1` 로 기동한다(`6672867`). 적용 전에는 기동 때 모델 허브 확인 요청이 있었고(gpu.log 경고 22회, 사례 정보 없음), 적용 뒤 재기동(2026-10-31 18:56:50)에서 새 경고는 0이었다(20:5x 재측정: 경고 수 22 그대로·마지막 경고는 직전 프로세스, 현재 프로세스 환경에 `HF_HUB_OFFLINE=1`, `evidence/infra-2026-10-31-night.md` 4절).

## 평가(합성 데이터)
진단 재현율·대상 오적용, 서류 단서 정밀도·재현율, 가드 거절·오거절(단일 질문·다턴 시뮬레이션), 근거 답 정답률·근거 없는 문장 비율, 레드팀 공격 성공률, 근거 조문 원문 일치율. 수치와 재현 명령은 `EVIDENCE.md`.

## 미확인·전제
- hk-gpu `0.0.0.0:8000` 은 **내부망(10.1.1.33)뿐 아니라 SSL-VPN 클라이언트에서도 닿는다**(2026-10-31 20:55, VPN 노트북에서 TCP 연결·`GET /health` 200, `evidence/infra-2026-10-31-night.md` 7절). NCP 콘솔(ACG)은 오너가 쓸 수 없어(규칙 원문도 제공 정보 없음) **2026-11-01 부터 GPU API 자체가 토큰을 요구한다**: `GET /health`(참·거짓만) 밖의 모든 경로는 토큰 없이 401(VPN 노트북·hk-ops 실측, `evidence/deploy-2026-11-01.md`). 토큰은 두 서버의 `~/.config/sumeunjip/gpu_token`(600)에만 있다. 포트 자체는 여전히 VPN 에서 닿으므로 `/health` 의 참·거짓 값은 VPN 사용자에게 보인다. vLLM 8001·앱 8080 은 127.0.0.1 바인드라 VPN 에서 닫혀 있다.
- 서비스는 TLS 없이 ssh 터널로만 시연한다. 공개 배포에는 TLS·속도 제한(전 엔드포인트)·인증 정책을 따로 정해야 한다.
- 조사 문서 10절의 미확인 22건(결정서·고지문에 새 주소가 적히는지 등)은 지식표 `confidence` 에 반영(확인 49·부분확인 31·미확인 4).
