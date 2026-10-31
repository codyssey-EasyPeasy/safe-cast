#!/usr/bin/env python3
"""data/knowledge.json 빌더 (Lane K).

조사 문서(research/2026-10-31-정보조사.md) 2.1~2.6 표에서 기계적 칸(경로·막는 제도·신청처·서류·한계·확인도)을
읽고, 상담 판단이 필요한 칸(피해 유형·상황 조건·긴급도·시점·통지 경고·상태·근거 조문)은 아래 OV 표로 덮는다.
plain 은 build_plain.py 가 채운다(기존 knowledge.json 에 plain 이 있으면 보존).

    python3 tools/build_knowledge.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESEARCH = ROOT / "research" / "2026-10-31-정보조사.md"
OUT = ROOT / "data" / "knowledge.json"
VERSION = "2026-10-31"

AREA = {"A": "행정", "J": "소송·수사", "S": "학적·보육", "L": "차량·우편·통신", "D": "디지털·위치", "P": "피신·지원"}

FLAGS = [
    ("moved_recently", "최근 이사(또는 이사 예정)", "주거"),
    ("same_registration_address", "가해자와 주민등록지가 같음", "주거"),
    ("shelter_needed", "피신처·임시 거처가 필요함(보호시설 포함)", "주거"),
    ("own_property_or_lease_registration", "본인 명의 부동산 등기·임차권등기·임대차 계약 예정", "주거"),
    ("region_seoul", "서울에 거주(또는 서울 지원 이용)", "주거"),
    ("lawsuit", "민사·가사 소송 중이거나 예정(이혼·양육·손해배상 등)", "소송·수사"),
    ("abuser_is_plaintiff", "가해자가 원고·채권자로 먼저 소송을 냄", "소송·수사"),
    ("criminal_case", "고소·신고 후 수사·재판 중", "소송·수사"),
    ("protection_order", "접근금지·보호명령 등 조치가 있거나 예정", "소송·수사"),
    ("children_school", "자녀가 학교에 다님(입학·전학 예정 포함)", "자녀"),
    ("children_daycare", "자녀가 어린이집·유치원에 다님", "자녀"),
    ("child_victim", "자녀도 학대·폭력 피해를 입음", "자녀"),
    ("vehicle", "본인 명의 차량이 있음", "차량"),
    ("shared_vehicle_or_abuser_owned", "공동명의 차량이거나 가해자 명의 차량을 씀", "차량"),
    ("mail_old_address", "옛 주소(가해자 거주지)로 우편물이 감", "우편·통신"),
    ("parcel_delivery", "택배·배달을 받음", "우편·통신"),
    ("abuser_named_phone_or_family_plan", "가해자 명의 휴대폰이거나 가족결합 요금제", "우편·통신"),
    ("shared_accounts_or_location_sharing", "계정·비밀번호·위치를 가해자와 공유했거나 공유 중", "디지털"),
    ("device_tampering_suspected", "휴대폰 등 기기 감시가 의심됨", "디지털"),
    ("bluetooth_tag_suspected", "위치 태그(추적기) 부착이 의심됨", "디지털"),
    ("photos_social", "사진·SNS를 게시함", "디지털"),
    ("posted_online", "주소·직장 등 신상이 온라인에 게시·유포됨", "디지털"),
    ("intimate_images", "불법촬영물·딥페이크 피해가 있음", "디지털"),
    ("workplace_known", "가해자가 직장을 앎", "생활·기타"),
    ("company_officer", "본인이 법인 대표이사·임원임", "생활·기타"),
    ("foreign_national", "외국인(결혼이민자 등) 피해자", "생활·기타"),
    ("health_insurance_dependent", "가해자의 건강보험 피부양자이거나 같은 세대", "생활·기타"),
    ("welfare_or_pension", "기초생활·국민연금 등 급여를 신청·수급", "생활·기타"),
    ("id_number_leaked", "주민등록번호가 가해자에게 알려짐", "생활·기타"),
    ("election_period", "선거를 앞두고 있음(선거인명부 작성 기간)", "생활·기타"),
]

# 법령명 -> (현행 시행일, 법령 URL, 조문 URL 접두(조사 문서 12절에 조문 링크가 있을 때), 조문 링크가 있는 조 목록)
_P = "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq="
_R = "https://www.law.go.kr/LSW/lsInfoR.do?lsiSeq="
_L = "https://www.law.go.kr/법령/"
LAWS: dict[str, tuple[str, str, str | None, set[str]]] = {
    "주민등록법": ("2025-07-22", _P + "268555&efYd=20250722", _L + "주민등록법/", {"제29조"}),
    "주민등록법 시행령": ("2026-10-29", _P + "285733&efYd=20261029", _L + "주민등록법%20시행령/", {"제47조의2"}),
    "주민등록법 시행규칙": ("2026-10-29", _R + "285757&efYd=20261029&chrClsCd=010202&ancYnChk=0", None, set()),
    "가족관계의 등록 등에 관한 법률": ("2026-10-02", _R + "290741&efYd=20261002&chrClsCd=010202&ancYnChk=0", None, set()),
    "가족관계의 등록 등에 관한 규칙": ("2025-07-19", _R + "272659&efYd=20250719&chrClsCd=010202&ancYnChk=0", None, set()),
    "가족관계등록예규 제653호": ("2026-10-07", "https://www.law.go.kr/LSW/admRulInfoR.do?admRulSeq=2200000109345&chrClsCd=010201", None, set()),
    "가정폭력범죄의 처벌 등에 관한 특례법": ("2026-10-02", _P + "290603&efYd=20261002", _L + "가정폭력범죄의처벌등에관한특례법/",
                                {"제8조", "제8조의2", "제18조", "제29조", "제29조의2", "제55조의2", "제55조의3", "제55조의4", "제63조"}),
    "가정폭력방지 및 피해자보호 등에 관한 법률": ("2026-07-08", _P + "285393&efYd=20260708",
                                    _L + "가정폭력방지%20및%20피해자보호%20등에%20관한%20법률/", {"제4조", "제8조", "제9조", "제16조"}),
    "가정폭력방지 및 피해자보호 등에 관한 법률 시행령": ("2026-07-08", _P + "288019&efYd=20260708", None, set()),
    "스토킹범죄의 처벌 등에 관한 법률": ("2026-10-02", _P + "289995&efYd=20261002", _L + "스토킹범죄의처벌등에관한법률/",
                              {"제2조", "제4조", "제7조", "제8조", "제9조", "제10조", "제17조의2", "제17조의3", "제17조의4", "제20조"}),
    "스토킹범죄의 처벌 등에 관한 법률(법률 제21556호)": ("2027-04-22", _P + "285471&efYd=20270422", None, set()),
    "스토킹방지 및 피해자보호 등에 관한 법률": ("2026-08-13", _P + "285901&efYd=20260813",
                                  _L + "스토킹방지%20및%20피해자보호%20등에%20관한%20법률/", {"제2조", "제3조", "제12조", "제15조"}),
    "스토킹방지 및 피해자보호 등에 관한 법률 시행령": ("2026-08-13", _P + "288675&efYd=20260813", None, set()),
    "성폭력방지 및 피해자보호 등에 관한 법률": ("2026-10-02", _P + "290759&efYd=20261002",
                                  _L + "성폭력방지%20및%20피해자보호%20등에%20관한%20법률/", {"제30조"}),
    "성폭력방지 및 피해자보호 등에 관한 법률 시행령": ("2026-07-01", _P + "286845&efYd=20260701", None, set()),
    "성폭력범죄의 처벌 등에 관한 특례법": ("2026-10-02", _L + "성폭력범죄의처벌등에관한특례법", _L + "성폭력범죄의처벌등에관한특례법/", {"제23조", "제24조"}),
    "민사소송법": ("2026-10-02", _L + "민사소송법", _L + "민사소송법/", {"제162조", "제163조", "제163조의2", "제184조", "제185조", "제208조"}),
    "민사소송규칙": ("2026-10-12", _L + "민사소송규칙", _L + "민사소송규칙/", {"제37조의3", "제38조"}),
    "민사판결서 열람 및 복사에 관한 규칙": ("2023-01-01", _L + "민사판결서열람및복사에관한규칙/제4조", _L + "민사판결서열람및복사에관한규칙/", {"제4조"}),
    "가사소송법": ("2026-01-01", _L + "가사소송법", _L + "가사소송법/", {"제10조", "제10조의2", "제12조", "제34조"}),
    "가정보호심판규칙": ("2024-07-26", _L + "가정보호심판규칙", _L + "가정보호심판규칙/", {"제3조", "제43조", "제69조", "제70조", "제71조", "제81조", "제87조"}),
    "형사소송법": ("2026-10-02", _L + "형사소송법", _L + "형사소송법/", {"제35조", "제59조의2", "제59조의3", "제266조의3", "제294조의3"}),
    "형사 판결서 등의 열람 및 복사에 관한 규칙": ("2019-01-01", _L + "형사판결서등의열람및복사에관한규칙/제4조", _L + "형사판결서등의열람및복사에관한규칙/", {"제4조"}),
    "재판기록 열람·복사 규칙": ("2025-10-10", _L + "재판기록열람ㆍ복사규칙/제7조", _L + "재판기록열람ㆍ복사규칙/", {"제7조"}),
    "특정범죄신고자 등 보호법": ("2026-10-02", _L + "특정범죄신고자등보호법", _L + "특정범죄신고자등보호법/", {"제7조", "제9조", "제11조", "제13조", "제13조의2"}),
    "검사와 사법경찰관의 상호협력과 일반적 수사준칙에 관한 규정": ("2026-10-02", _L + "검사와사법경찰관의상호협력과일반적수사준칙에관한규정/제69조",
                                              _L + "검사와사법경찰관의상호협력과일반적수사준칙에관한규정/", {"제69조"}),
    "경찰수사규칙": ("2026-10-02", _L + "경찰수사규칙/제87조", _L + "경찰수사규칙/", {"제87조"}),
    "피해자 보호 및 지원에 관한 규칙(경찰청훈령)": ("2025-12-31", "https://www.law.go.kr/LSW/admRulInfoP.do?admRulSeq=2100000273166", None, set()),
    "범죄피해자 보호법": ("2026-10-02", _L + "범죄피해자보호법", _L + "범죄피해자보호법/", {"제8조의2"}),
    "전자장치 부착 등에 관한 법률": ("2026-10-02", _L + "전자장치부착등에관한법률", None, set()),
    "소송촉진 등에 관한 특례법": ("2026-06-02", _L + "소송촉진등에관한특례법", _L + "소송촉진등에관한특례법/", {"제26조", "제28조"}),
    "공탁법": ("2026-10-02", _L + "공탁법/제5조의2", _L + "공탁법/", {"제5조의2"}),
    "개인정보 보호법": ("2026-09-11", _P + "283839&efYd=20260911", _L + "개인정보%20보호법/", {"제3조", "제23조", "제28조의8", "제29조", "제58조의2", "제59조"}),
    "위치정보의 보호 및 이용 등에 관한 법률": ("2025-10-01", _P + "277359&efYd=20251001", _L + "위치정보의%20보호%20및%20이용%20등에%20관한%20법률/", {"제9조", "제15조"}),
    "정보통신망 이용촉진 및 정보보호 등에 관한 법률": ("2026-10-02", _L + "정보통신망이용촉진및정보보호등에관한법률", None, set()),
    "통신비밀보호법": ("2026-10-02", _L + "통신비밀보호법", None, set()),
    "전기통신사업법": ("2026-10-01", _P + "285185&efYd=20261001", None, set()),
    "국민건강보험법 시행규칙": ("2026-09-28", _R + "290599&efYd=20260928&chrClsCd=010202&ancYnChk=0", None, set()),
    "국민건강보험법 시행령": ("2026-10-01", _R + "289701&efYd=20261001&chrClsCd=010202&ancYnChk=0", None, set()),
    "국민연금법": ("2026-10-20", _R + "291347&efYd=20261020&chrClsCd=010202&ancYnChk=0", None, set()),
    "공공기관의 정보공개에 관한 법률": ("2023-11-17", _R + "251019&efYd=20231117&chrClsCd=010202&ancYnChk=0", None, set()),
    "출입국관리법": ("2026-10-02", _R + "290729&efYd=20261002&chrClsCd=010202&ancYnChk=0", None, set()),
    "출입국관리법 시행규칙": ("2026-10-23", _R + "291289&efYd=20261023&chrClsCd=010202&ancYnChk=0", None, set()),
    "국민기초생활 보장법": ("2025-10-01", _R + "276653&efYd=20251001&chrClsCd=010202&ancYnChk=0", None, set()),
    "공직선거법": ("2026-10-23", _R + "285629&efYd=20261023&chrClsCd=010202&ancYnChk=0", None, set()),
    "공직선거관리규칙": ("2026-10-02", _R + "290961&efYd=20261002&chrClsCd=010202&ancYnChk=0", None, set()),
    "부동산등기법": ("2025-01-31", _R + "265377&efYd=20250131&chrClsCd=010202&ancYnChk=0", None, set()),
    "부동산등기규칙": ("2025-08-01", _R + "266847&efYd=20250801&chrClsCd=010202&ancYnChk=0", None, set()),
    "아동복지법": ("2026-10-02", _P + "290207&efYd=20261002", None, set()),
    "아동복지법 시행령": ("2026-08-04", _P + "288363&efYd=20260804", None, set()),
    "아동학대범죄의 처벌 등에 관한 특례법": ("2026-10-02", _P + "289809&efYd=20261002", None, set()),
    "유아교육법": ("2026-05-19", _P + "286061&efYd=20260519", None, set()),
    "초·중등교육법": ("2026-10-29", _P + "285599&efYd=20261029", None, set()),
    "영유아보육법": ("2026-05-19", _P + "286057&efYd=20260519", None, set()),
    "영유아보육법 시행규칙": ("2026-05-12", _P + "286013&efYd=20260512", None, set()),
    "민법": ("2026-03-17", _P + "284415&efYd=20260317", None, set()),
    "자동차관리법": ("2026-10-02", _P + "290737&efYd=20261002", None, set()),
    "자동차등록령": ("2026-06-03", _P + "286481&efYd=20260603", None, set()),
    "자동차등록규칙": ("2026-06-03", _P + "286693&efYd=20260603", None, set()),
    "주택임대차보호법": ("2026-01-02", _P + "276291&efYd=20260102", None, set()),
    "주택임대차보호법 시행령": ("2026-07-01", _P + "287183&efYd=20260701", None, set()),
    "상법": ("2026-09-10", _P + "273629&efYd=20260910", None, set()),
    "우편법": ("2026-03-31", _P + "285143&efYd=20260331", None, set()),
    "우편법 시행령": ("2025-07-15", _P + "272793&efYd=20250715", None, set()),
    "우편법 시행규칙": ("2026-04-01", _P + "273389&efYd=20260401", None, set()),
    # 법령이 아닌 공식 안내(조사 문서 12.2의 URL)
    "국민건강보험공단 민원 안내": ("", "https://www.nhis.or.kr/nhis/minwon/minwonServiceBoard.do?mode=view&articleNo=10945798", None, set()),
    "법무부 보도자료(위치 알림 앱)": ("2026-05-27", "https://www.moj.go.kr/bbs/moj/182/606921/artclView.do", None, set()),
    "법무부 TF 보도자료": ("2026-07-13", "https://www.moj.go.kr/bbs/moj/182/608429/artclView.do", None, set()),
    "개인정보보호위원회 보도자료(택배 운송장)": ("2025-09-11", "https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?bbsId=BS074&mCode=C020010000&nttId=11488", None, set()),
    "Apple 개인 안전 사용 설명서": ("", "https://help.apple.com/pdf/personal-safety/ko_KR/personal-safety-user-guide-ko_KR.pdf", None, set()),
    "Google 고객센터(기기·위치 공유 관리)": ("", "https://support.google.com/accounts/answer/3067630?hl=ko", None, set()),
    "Google Android 도움말(알 수 없는 추적기)": ("", "https://support.google.com/android/answer/13658562?hl=ko", None, set()),
    "성평등가족부 1366 안내": ("", "https://www.mogef.go.kr/sp/hrp/sp_hrp_f002.do", None, set()),
    "성평등가족부 스토킹·교제폭력 지원 안내": ("", "https://www.mogef.go.kr/sp/hrp/sp_hrp_f018.do", None, set()),
    "성평등가족부 해바라기센터 안내": ("", "https://www.mogef.go.kr/sp/hrp/sp_hrp_f011.do", None, set()),
    "마이홈포털 주거취약계층 주거지원": ("", "https://www.myhome.go.kr/hws/portal/cont/selectHousingSupVulnerableGroupsView.do", None, set()),
    "서울시 스토킹 피해자 지원 누리집": ("2026-04-20", "https://news.seoul.go.kr/welfare/archives/559109", None, set()),
}

# 법령 약칭 (짧게 쓰기 위함)
JU, JUR, JUK = "주민등록법", "주민등록법 시행령", "주민등록법 시행규칙"
GA, GAK, YE = "가족관계의 등록 등에 관한 법률", "가족관계의 등록 등에 관한 규칙", "가족관계등록예규 제653호"
GP, GB, GBR = "가정폭력범죄의 처벌 등에 관한 특례법", "가정폭력방지 및 피해자보호 등에 관한 법률", "가정폭력방지 및 피해자보호 등에 관한 법률 시행령"
SP, SP2, SB, SBR = "스토킹범죄의 처벌 등에 관한 법률", "스토킹범죄의 처벌 등에 관한 법률(법률 제21556호)", "스토킹방지 및 피해자보호 등에 관한 법률", "스토킹방지 및 피해자보호 등에 관한 법률 시행령"
SEB, SEBR, SEP = "성폭력방지 및 피해자보호 등에 관한 법률", "성폭력방지 및 피해자보호 등에 관한 법률 시행령", "성폭력범죄의 처벌 등에 관한 특례법"
MS, MSK, GS, GSK = "민사소송법", "민사소송규칙", "가사소송법", "가정보호심판규칙"
HS, TS = "형사소송법", "특정범죄신고자 등 보호법"
PI, LOC = "개인정보 보호법", "위치정보의 보호 및 이용 등에 관한 법률"
POL = "피해자 보호 및 지원에 관한 규칙(경찰청훈령)"
APPLE = "Apple 개인 안전 사용 설명서"

ALL = ["any"]
F, S, D, X = "family", "stalking", "dating", "sexual"

NW_A1 = ("제한을 신청하면 그 사유가 제한대상자(가해자)에게 서면으로 통지됩니다(주민등록법 제29조 제7·8항 후단). "
         "통지가 가기 전에 안전계획을 세우고, 통지 내용에 새 주소가 드러나지 않는지 담당자에게 확인하세요.")
NW_A8 = ("교부제한을 하면 그 사실이 교부제한대상자(가해자)에게 고지됩니다(가족관계등록예규 제653호 제8조). "
         "고지 시점에 맞춰 안전계획을 먼저 세우세요.")
NW_J10 = ("접근금지 장소를 새 주거로 바꾸면 변경된 조치 내용이 행위자에게 고지됩니다(스토킹처벌법 제10조 제4항·제7조 제5항, 가정폭력은 결정 고지). "
          "고지문에 새 주소가 적히는지는 확인되지 않았으므로, 신청 전에 기재·고지 방식을 법원·경찰에 먼저 확인하고 새 주소는 별지로 내며 가림을 요청하세요.")
NW_J13 = ("긴급응급조치·잠정조치 결정과 그 변경 내용은 스토킹행위자에게 고지됩니다(스토킹처벌법 제7조 제5항, 제10조 제2·4항). "
          "결정서에 피해자 주거 기재를 생략하는 근거는 확인되지 않았으니 기재 방식을 먼저 확인하세요.")
NW_J12 = ("청구서 부본이 행위자에게 송달되고 결정서에는 원칙적으로 피해자 주거가 적힙니다. "
          "청구서에 주거·연락처 가림(가정보호심판규칙 제81조 제3항)과 결정서 주거 기재 생략(제3조 제3항)을 함께 요청하세요.")
NW_J14 = ("시행 후에도 결정과 그 변경 내용이 행위자에게 고지되는 구조는 잠정조치와 같습니다(제17조의6 제9항, 제10조 준용). "
          "고지문의 주소 기재 방식을 먼저 확인하세요.")
NW_L7 = ("문자 수신이 안 되면 통지가 주민등록 주소로 등기 발송될 수 있습니다. 그 주소가 가해자와 같은 집이면 가해자가 알게 될 수 있으니 "
         "주민등록 정리 후에 신청하세요.")
NW_DEV = "기기를 점검·초기화하거나 앱을 지우면 가해자가 알아챌 수 있고 증거가 사라질 수 있습니다. 안전계획과 증거 보전을 먼저 하세요."
NW_D2 = "장치를 떼거나 차량 앱 연동을 해제하면 가해자가 알아챌 수 있습니다. 장치는 폐기하지 말고 안전계획을 세운 뒤 경찰과 함께 처리하세요."
NW_D3 = "태그를 비활성화하면 가해자가 알아챌 수 있습니다. 화면 캡처와 실물 보전을 먼저 하고 안전계획을 세운 뒤 처리하세요."
NW_D4 = "동의를 철회하거나 일시중지하면 위치 공유가 끊긴 것을 가해자가 알 수 있습니다. 철회하면 기록이 파기되므로 제공내역 열람을 먼저 하세요."
NW_D5 = "공유를 끊거나 비밀번호를 바꾸면 가해자가 알아챌 수 있습니다. 안전계획을 세운 뒤 한 번에 정리하세요."


def b(*entries):
    """(law, article) 튜플 목록 -> basis. 첫 항목이 primary."""
    return [(law, art, i == 0) for i, (law, art) in enumerate(entries)]


# id: v=victim_types, all/any=triggers, u=urgency, tm=timing, b=basis, st=status, nw=notify_warning, exp=exposure 덮어쓰기
OV: dict[str, dict] = {
    # ---- A 행정 ----
    "A1": dict(v=[F], u=1, tm="전입신고와 같은 방문에서(전입 직후 제한 전 공백을 줄인다)", nw=NW_A1,
               exp="가족 자격으로 발급되는 주민등록 등·초본에 새 주소가 실린다",
               b=b((JU, "제29조 제6항"), (JU, "제29조 제7항"), (JU, "제29조 제8항"), (JUR, "제47조의2"), (JUK, "제13조의2"))),
    "A2": dict(v=ALL, any=["moved_recently", "lawsuit"], u=2, tm="전입 직후 주민센터에 위험 사정을 알려 둔다",
               exp="소송·이해관계를 이유로 한 제3자 초본 발급에서 새 주소가 나갈 수 있다",
               b=b((JU, "제29조 제5항"), (JU, "제29조 제2항"))),
    "A3": dict(v=ALL, u=2, tm="전입신고 때 함께", b=b((JUR, "제47조 제8항"), (JUK, "제6조의2 제1항"))),
    "A4": dict(v=[F], u=3, tm="제한 신청 때 해제 사유를 함께 안내", b=b((JU, "제29조 제9항"), (JUR, "제47조의2 제2항"), (JUK, "제13조의2"))),
    "A5": dict(v=ALL, any=["id_number_leaked"], u=2, tm="가족관계 교부 제한(A8)을 먼저 또는 함께 신청한 뒤",
               b=b((JU, "제7조의4 제1항"), (JU, "제7조의5 제4항"), (JUR, "제12조의3"), (JUR, "제12조의6"))),
    "A6": dict(v=[F], any=["moved_recently", "same_registration_address", "shelter_needed"], u=1,
               tm="전입신고(이사 후 14일 이내) 당일, 같은 창구에서",
               b=b((JU, "제16조 제1항"), (JU, "제29조 제6항"), (JUR, "제30조 제2항"))),
    "A7": dict(v=ALL, any=["moved_recently"], u=2, st="none", tm="입주·계약 전에 소유·임대 관계를 점검",
               exp="건물 관련 확인 서류에 그 건물 전입 세대주·동거인의 성명·전입일자가 표시될 수 있다",
               b=b((JU, "제29조의2"), (JU, "제16조의2"), (JUR, "제49조의2"), ("출입국관리법", "제88조의3"))),
    "A8": dict(v=[F], u=1, tm="주민등록 제한(A1)과 같은 날, 주민번호 변경 전 또는 함께", nw=NW_A8,
               exp="가족 자격으로 발급·열람되는 가족관계 증명서에 주민번호·자녀·혼인 등 단서가 실린다",
               b=b((GA, "제14조 제8항"), (GA, "제14조의2 제3항"), (GA, "제15조 제2항"), (GAK, "제25조의3"), (YE, "제2조"), (YE, "제8조"))),
    "A9": dict(v=[F], u=2, tm="A8 신청서와 함께", b=b((GA, "제15조의2"), (YE, "제4조"), (GAK, "제25조의3 제3항"))),
    "A10": dict(v=[F], u=3, tm="A8 신청 시 자동 적용 여부를 확인", b=b((GAK, "제27조 제1항"), (YE, "제6조"))),
    "A11": dict(v=ALL, any=["health_insurance_dependent"], u=2, tm="이사 직후",
                b=b(("국민건강보험법 시행규칙", "제2조 제3항"), ("국민건강보험법 시행령", "제43조"), ("국민건강보험공단 민원 안내", ""))),
    "A12": dict(v=ALL, any=["health_insurance_dependent"], u=3, st="none", tm="해당 시 공단·심평원에 미리 문의",
                exp="병원·약국 이용 내역으로 생활 반경이 추정될 수 있다",
                b=b((PI, "제35조 제4항"))),
    "A13": dict(v=ALL, any=["welfare_or_pension"], u=3, st="none", tm="분할·유족연금 절차 전에 문의",
                b=b(("국민연금법", "제64조"), (PI, "제35조 제4항"))),
    "A14": dict(v=ALL, any=["children_school", "children_daycare", "child_victim", "moved_recently"], u=2,
                tm="학교·병원·공공기관에 등록할 때 서면으로 미리",
                exp="가해자 본인 기록이나 자녀 기록 열람 과정에서 피해자 주소가 섞여 나갈 수 있다",
                b=b((PI, "제35조 제4항"), (PI, "제38조 제2항"))),
    "A15": dict(v=ALL, any=["mail_old_address", "moved_recently"], u=3, tm="제3자 통지를 받으면 3일 안에",
                b=b(("공공기관의 정보공개에 관한 법률", "제21조"), ("공공기관의 정보공개에 관한 법률", "제9조 제1항"), ("공공기관의 정보공개에 관한 법률", "제11조 제3항"))),
    "A16": dict(v=ALL, all=["foreign_national"], u=2, st="none", tm="체류지 변경 신고 전에 상담",
                exp="외국인등록 사실증명이 법정 예외 요건에 따라 가족 등에게 발급될 수 있다",
                b=b(("출입국관리법", "제88조 제2항"), ("출입국관리법 시행규칙", "제75조"))),
    "A17": dict(v=ALL, any=["welfare_or_pension"], u=3, tm="급여 신청 상담 때",
                b=b(("국민기초생활 보장법", "제8조의2 제2항"))),
    "A18": dict(v=ALL, any=["election_period"], u=2, st="none", tm="선거인명부 작성·열람기간 전",
                exp="선거인명부에 성명·주소 등이 실리고 열람기간에 공개된다",
                b=b(("공직선거법", "제40조 제2항"), ("공직선거법", "제37조 제2항"), ("공직선거관리규칙", "제13조 제1항"))),
    "A19": dict(v=ALL, any=["election_period"], u=3, st="none", tm="선거 기간",
                exp="후보자 측에 교부되는 선거인명부 사본에 주소가 포함된다",
                b=b(("공직선거법", "제46조"))),
    "A20": dict(v=ALL, any=["own_property_or_lease_registration"], u=1, st="none", tm="등기 신청 전",
                exp="등기기록에 권리자의 주소가 기록되고 공개된다",
                b=b(("부동산등기법", "제48조 제2항"), ("부동산등기법", "제19조 제1항"), ("부동산등기규칙", "제32조 제1항"))),
    "A21": dict(v=ALL, any=["own_property_or_lease_registration"], u=1, st="none", tm="임차권등기명령 신청 전",
                b=b(("부동산등기법", "제74조"), ("주택임대차보호법", "제3조의3"))),
    "A22": dict(v=ALL, any=["own_property_or_lease_registration"], u=3, tm="임대차 계약 전 임대인 관계를 점검",
                b=b(("주택임대차보호법", "제3조의6 제3항"), ("주택임대차보호법 시행령", "제5조"))),
    "A23": dict(v=ALL, any=["company_officer"], u=2, st="none", tm="설립·변경 등기 전",
                b=b(("상법", "제317조 제2항"))),
    # ---- J 소송·수사 ----
    "J1": dict(v=ALL, all=["lawsuit"], u=1, tm="소장 접수·답변서 제출 때 함께(송달 전)",
               exp="당사자 표시의 주소가 소송 서류 송달로 상대방에게 전달된다",
               b=b((MS, "제163조 제2항"), (MSK, "제38조 제3항"), (GS, "제12조"))),
    "J2": dict(v=ALL, all=["lawsuit"], u=1, tm="첫 서면을 낼 때", b=b((MS, "제184조"), (MS, "제185조"))),
    "J3": dict(v=ALL, all=["lawsuit"], u=1, tm="증거를 내기 전(주소를 지워 제출)", b=b((MS, "제163조 제2항"), (MS, "제162조"), (MSK, "제38조 제1항"))),
    "J4": dict(v=ALL, all=["lawsuit"], u=3, tm="통지를 받은 날부터 2주 이내", b=b((MS, "제162조 제2항"), (MSK, "제37조의3"))),
    "J5": dict(v=ALL, all=["lawsuit"], u=1, tm="소 제기 때(판결·조서 송달 전)", b=b((MS, "제163조 제2항"), (MS, "제208조 제1항"))),
    "J6": dict(v=ALL, any=["lawsuit", "criminal_case"], u=3, tm="판결 선고 전후",
               b=b((MS, "제163조의2"), ("민사판결서 열람 및 복사에 관한 규칙", "제4조"), (HS, "제59조의3"), ("형사 판결서 등의 열람 및 복사에 관한 규칙", "제4조"))),
    "J7": dict(v=[F, D], all=["lawsuit"], u=1, tm="가사 소장 접수 때",
               b=b((GS, "제12조"), (GS, "제10조"), (GS, "제10조의2"), (GS, "제34조"))),
    "J8": dict(v=[F], any=["protection_order"], u=1, tm="보호명령 청구서를 낼 때", b=b((GSK, "제81조 제3항"), (GSK, "제69조"), (GSK, "제87조"))),
    "J9": dict(v=[F], any=["protection_order"], u=1, tm="보호명령 청구서를 낼 때", b=b((GSK, "제3조 제3항"))),
    "J10": dict(v=[F, S], all=["protection_order", "moved_recently"], u=1, tm="변경 신청 전에 고지 방식부터 확인", nw=NW_J10,
                exp="장소 기준 접근금지를 새 주거로 바꾸면 변경된 조치 내용이 행위자에게 고지된다",
                b=b((SP, "제10조 제4항"), (SP, "제7조 제5항"), (GP, "제29조의2"), (SP, "제17조의3"), (SP, "제20조"))),
    "J11": dict(v=[F], u=2, tm="위험이 있으면 즉시(112)", b=b((GP, "제29조"), (GP, "제8조"), (GP, "제8조의2"), (GP, "제63조"))),
    "J12": dict(v=[F], u=2, tm="형사 절차와 별개로 언제든", nw=NW_J12,
                b=b((GP, "제55조의2"), (GP, "제55조의3"), (GP, "제55조의4"), (GSK, "제69조"))),
    "J13": dict(v=[S, D], u=2, tm="위험이 있으면 즉시(112)", nw=NW_J13,
                b=b((SP, "제9조"), (SP, "제4조"), (SP, "제8조"), (SP, "제3조"), (SP, "제20조"))),
    "J14": dict(v=[S], any=["criminal_case", "protection_order"], u=2, st="pending", pe="2027-04-22",
                tm="2027-04-22 시행 후, 미신청 통지를 받은 날부터 90일 이내", nw=NW_J14,
                b=b((SP2, "제17조의6"), (SP2, "제17조의5"))),
    "J15": dict(v=[S], any=["protection_order", "criminal_case"], u=2, tm="잠정조치 신청 단계에서 요청",
                b=b((SP, "제9조 제1항"), ("전자장치 부착 등에 관한 법률", "제31조의6"), ("법무부 보도자료(위치 알림 앱)", ""))),
    "J16": dict(v=[F, S, X], any=["lawsuit", "criminal_case", "protection_order"], u=2, tm="출석 일정이 잡히면 미리",
                b=b((GP, "제55조의2 제5항"), (SP, "제17조의2"), (TS, "제13조"), (TS, "제13조의2"), (SEP, "제23조"))),
    "J17": dict(v=ALL, any=["criminal_case", "protection_order"], u=2, tm="신고 직후",
                b=b((POL, "제28조"), (POL, "제29조"), (POL, "제33조"), ("범죄피해자 보호법", "제9조"))),
    "J18": dict(v=ALL, any=["criminal_case"], u=2, tm="고위험으로 판단될 때", b=b(("법무부 TF 보도자료", ""))),
    "J19": dict(v=ALL, all=["criminal_case"], u=1, tm="고소 접수나 첫 조서 작성 때",
                b=b((TS, "제7조"), (TS, "제9조"), (SEP, "제23조"), (POL, "제23조"))),
    "J20": dict(v=ALL, all=["criminal_case"], u=1, tm="고소할 때 비실명 요청",
                b=b(("검사와 사법경찰관의 상호협력과 일반적 수사준칙에 관한 규정", "제69조"), ("경찰수사규칙", "제87조 제5항"))),
    "J21": dict(v=ALL, all=["criminal_case"], u=2, tm="기소 후 열람·복사 신청 전",
                b=b((HS, "제35조 제3항"), ("재판기록 열람·복사 규칙", "제7조 제3항"), (HS, "제266조의3"), (GSK, "제43조"))),
    "J22": dict(v=ALL, all=["criminal_case"], u=3, tm="사건 확정 후", b=b((HS, "제59조의2 제2항"))),
    "J23": dict(v=ALL, all=["criminal_case"], u=2, tm="증인 소환을 받으면", b=b((HS, "제294조의3"), (TS, "제11조"))),
    "J24": dict(v=ALL, all=["criminal_case"], u=1, tm="배상명령 신청서를 낼 때(부본 송달 전)", b=b(("소송촉진 등에 관한 특례법", "제28조"), ("소송촉진 등에 관한 특례법", "제26조"))),
    "J25": dict(v=ALL, all=["criminal_case"], u=2, tm="합의·공탁 연락이 오면", b=b(("공탁법", "제5조의2"), (TS, "제9조 제3항"))),
    "J26": dict(v=[F, S, X], any=["criminal_case", "protection_order", "posted_online"], u=3, tm="누설·공개를 알게 되면",
                b=b((GP, "제18조"), (SP, "제17조의3"), (SP, "제2조 제1호"), (SEP, "제24조"), (GP, "제64조"))),
    "J27": dict(v=ALL, any=["lawsuit", "criminal_case", "protection_order"], u=2, tm="절차를 시작하기 전",
                b=b((SP, "제17조의4"), (SEP, "제27조 제6항"), (SEB, "제7조의2"), (GSK, "제70조"), ("범죄피해자 보호법", "제8조의2"))),
    # ---- S 학적·보육 ----
    "S1": dict(v=[F], any=["children_school"], u=1, tm="전학·입학 절차를 밟기 전", b=b((GB, "제4조의4"), (GBR, "제1조의3"))),
    "S2": dict(v=[X], any=["children_school"], u=1, tm="전학·입학 절차를 밟기 전", b=b((SEB, "제7조"), (SEBR, "제4조"))),
    "S3": dict(v=[S, D], any=["children_school"], u=1, tm="전학·입학 절차를 밟기 전", b=b((SB, "제7조"), (SB, "제2조"), (SBR, "제4조"))),
    "S4": dict(v=[F], all=["child_victim"], any=["children_school", "children_daycare"], u=1, tm="취학·전학 전",
               b=b(("아동복지법", "제29조 제7항"), ("아동복지법 시행령", "제26조의4"))),
    "S5": dict(v=[F], any=["children_school", "children_daycare"], u=1, tm="전학·입소 전에 이전·새 학교(원)에 서면으로",
               exp="교직원·보육교직원을 통해 전학·입소 사실이 친권자에게 전달될 수 있다",
               b=b((GP, "제18조 제3항"), ("아동학대범죄의 처벌 등에 관한 특례법", "제35조 제3항"), ("유아교육법", "제2조"))),
    "S6": dict(v=ALL, any=["children_school"], u=2, tm="전학 직후 학교장에게",
               exp="보호자 자격의 학부모서비스에 재학 학교·출결이 표시된다",
               b=b(("초·중등교육법", "제30조의6 제1항"), (PI, "제35조 제4항"))),
    "S7": dict(v=ALL, any=["children_daycare"], u=2, tm="입소 신청 때", b=b(("유아교육법", "제17조의2"), ("영유아보육법", "제28조 제1항"), ("영유아보육법 시행규칙", "제29조"))),
    "S8": dict(v=[F], any=["children_school", "children_daycare", "child_victim"], u=2, tm="보호명령·친권 청구 때",
               b=b((GP, "제55조의2 제1항"), ("아동학대범죄의 처벌 등에 관한 특례법", "제47조"), ("민법", "제924조"), ("민법", "제924조의2"))),
    # ---- L 차량·우편·통신 ----
    "L1": dict(v=ALL, any=["vehicle", "shared_vehicle_or_abuser_owned"], u=2, st="none", tm="이사 전 차량 명의 정리",
               exp="자동차등록원부에 사용본거지(주소)가 기재된다",
               b=b(("자동차관리법", "제7조 제4항"), ("자동차등록규칙", "제10조 제1항"), ("자동차등록규칙", "제12조"))),
    "L2": dict(v=ALL, any=["vehicle", "shared_vehicle_or_abuser_owned"], u=1, st="none", tm="전입신고 전에 공동명의·가해자 명의 차량 정리",
               b=b(("자동차등록령", "제22조 제2항"), ("자동차관리법", "제12조의2"))),
    "L3": dict(v=ALL, any=["mail_old_address", "moved_recently"], u=2, tm="이사 직후", b=b(("우편법", "제31조의2"), ("우편법 시행규칙", "제110조"), ("우편법", "제51조의2"))),
    "L4": dict(v=ALL, any=["moved_recently", "mail_old_address", "shelter_needed"], u=2, tm="기관에 수령지를 적기 전",
               b=b(("우편법", "제37조"), ("우편법 시행령", "제43조"), ("우편법 시행규칙", "제122조"))),
    "L5": dict(v=ALL, any=["lawsuit", "criminal_case"], u=2, st="none", tm="내용증명을 보내기 전", b=b(("우편법 시행규칙", "제52조 제4항"))),
    "L6": dict(v=ALL, any=["parcel_delivery"], u=2, st="none", tm="이사 직후부터",
               b=b(("개인정보보호위원회 보도자료(택배 운송장)", ""), (SP, "제2조 제1호"))),
    "L7": dict(v=ALL, any=["abuser_named_phone_or_family_plan"], u=2, tm="새 거처 주민등록을 정리한 뒤", nw=NW_L7,
               b=b(("전기통신사업법", "제32조의6"))),
    "L8": dict(v=ALL, any=["moved_recently", "abuser_named_phone_or_family_plan", "shared_accounts_or_location_sharing"], u=2, st="none",
               tm="이사 전후 계약·계좌 정리", b=b(("자동차등록령", "제22조 제2항"))),
    # ---- D 디지털·위치 ----
    "D1": dict(v=ALL, any=["device_tampering_suspected"], u=2, tm="안전계획을 세운 뒤, 이사 전에", nw=NW_DEV,
               b=b((LOC, "제15조 제1항"), (LOC, "제40조"), ("정보통신망 이용촉진 및 정보보호 등에 관한 법률", "제48조"), ("통신비밀보호법", "제3조"))),
    "D2": dict(v=ALL, any=["shared_vehicle_or_abuser_owned", "device_tampering_suspected", "bluetooth_tag_suspected"], u=2,
               tm="안전계획을 세운 뒤, 이사 전에", nw=NW_D2,
               b=b((LOC, "제15조"), (LOC, "제40조"), (SP, "제2조 제1호"))),
    "D3": dict(v=ALL, any=["bluetooth_tag_suspected"], u=2, tm="안전계획을 세운 뒤", nw=NW_D3,
               b=b((APPLE, ""), ("Google Android 도움말(알 수 없는 추적기)", ""), (LOC, "제15조"))),
    "D4": dict(v=ALL, any=["shared_accounts_or_location_sharing", "abuser_named_phone_or_family_plan"], u=1,
               tm="이사 전(제공내역 열람 → 철회 순서)", nw=NW_D4,
               b=b((LOC, "제24조 제2항"), (LOC, "제19조"), (LOC, "제26조"))),
    "D5": dict(v=ALL, any=["shared_accounts_or_location_sharing"], u=1, tm="이사 전, 안전계획을 세운 뒤 한 번에", nw=NW_D5,
               b=b((APPLE, ""), ("Google 고객센터(기기·위치 공유 관리)", ""))),
    "D6": dict(v=ALL, any=["photos_social"], u=2, tm="이사 직후부터", b=b((APPLE, ""))),
    "D7": dict(v=ALL, any=["posted_online"], u=2, tm="게시를 알게 되면 즉시(증거 보전 후)",
               exp="주소·직장 같은 신상이 온라인에 게시·유포될 수 있다",
               b=b((SP, "제2조 제1호"), ("정보통신망 이용촉진 및 정보보호 등에 관한 법률", "제44조의2"), (SEB, "제7조의3"))),
    # 표의 대상은 '공통(혼인 유지 중 위험)'이나, 요청 자격이 배우자·2촌 친족에게만 있으므로 가정폭력 유형에만 적용한다.
    "D8": dict(v=[F], u=2, tm="피신 직후 119·112에 피해 사실을 알림",
               exp="긴급구조 요청 자격이 배우자·2촌 이내 친족에게도 있어 목적 외로 쓰일 위험이 있다",
               b=b((LOC, "제29조 제1항"), (LOC, "제39조"), (LOC, "제43조"))),
    # ---- P 피신·지원 ----
    "P1": dict(v=ALL, any=["shelter_needed"], u=1, tm="피신 직후", b=b((GB, "제4조의6"), ("성평등가족부 1366 안내", ""))),
    "P2": dict(v=[F, X, S], any=["shelter_needed"], u=2, tm="입소 때",
               b=b((GB, "제16조"), (GB, "제9조"), (SEB, "제30조"), (SB, "제15조"), (SB, "제12조"))),
    "P3": dict(v=[S, D], any=["shelter_needed"], u=1, tm="거처가 노출되면 즉시",
               b=b((SB, "제3조 제1항"), ("성평등가족부 스토킹·교제폭력 지원 안내", ""))),
    "P4": dict(v=ALL, any=["shelter_needed"], u=3, tm="시설 퇴소 전", b=b((GBR, "제4조의2"), ("범죄피해자 보호법", "제7조 제1항"), ("마이홈포털 주거취약계층 주거지원", ""))),
    "P5": dict(v=[X, F], u=2, tm="피해 직후", b=b((SEB, "제18조"), ("성평등가족부 해바라기센터 안내", ""))),
    "P6": dict(v=ALL, all=["intimate_images"], u=1, tm="유포를 알게 되면 즉시", b=b((SEB, "제7조의3"), (SEB, "제7조의4"))),
    "P7": dict(v=[F, S], any=["workplace_known"], u=2, tm="이사 전후 인사부서와 상담", b=b((SB, "제6조"), (GB, "제4조의5"))),
    "P8": dict(v=[S, D], all=["region_seoul"], u=2, tm="신고 직후", b=b(("서울시 스토킹 피해자 지원 누리집", ""))),
    "P9": dict(v=ALL, any=["shelter_needed"], u=2, tm="머물 곳이 없을 때", b=b(("범죄피해자 보호법", "제7조 제2항"))),
}


def clean(s: str) -> str:
    s = s.replace("**", "").strip()
    return re.sub(r"\s+", " ", s)


def parse_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in RESEARCH.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([AJSLDP]\d+)\s*\|", line)
        if not m:
            continue
        cells = [clean(c) for c in line.strip().strip("|").split("|")]
        if len(cells) != 10:
            raise SystemExit(f"{m.group(1)}: expected 10 cells, got {len(cells)}")
        rows[cells[0]] = cells
    return rows


def strip_case_refs(s: str) -> str:
    # 노출 칸의 사례 괄호(광주 2026-09 사건, [2차] 등)는 설명 문장에서 뺀다.
    s = re.sub(r"\((?:[^()]*사건|[^()]*\[2차\][^()]*|[^()]*미확인[^()]*)\)", "", s)
    return re.sub(r"\s+", " ", s).strip()


def conf(s: str) -> str:
    for c in ("부분확인", "미확인", "확인"):
        if s.startswith(c):
            return c
    raise SystemExit(f"bad confidence {s!r}")


def basis_entry(law: str, art: str, primary: bool) -> dict:
    if law not in LAWS:
        raise SystemExit(f"unknown law {law!r}")
    eff, law_url, prefix, listed = LAWS[law]
    base = re.match(r"^(제\d+조(?:의\d+)?)", art)
    url = prefix + base.group(1) if (prefix and base and base.group(1) in listed) else law_url
    return {"law": law, "article": art, "effective": eff, "url": url, "primary": primary}


def main() -> int:
    rows = parse_rows()
    old_plain = {}
    if OUT.exists():
        for it in json.loads(OUT.read_text(encoding="utf-8")).get("items", []):
            old_plain[it["id"]] = (it.get("plain", ""), it.get("plain_reviewed", False))
    items = []
    for iid, c in rows.items():
        o = OV[iid]
        _, path, exposure, measure, _basis_txt, _target, where, docs, limits, cf = c
        status = o.get("st", "in_force")
        plain, reviewed = old_plain.get(iid, ("", False))
        items.append({
            "id": iid,
            "area": AREA[iid[0]],
            "path": path,
            "exposure": o.get("exp") or strip_case_refs(exposure),
            "measure": measure,
            "basis": [basis_entry(*e) for e in o["b"]],
            "victim_types": o["v"],
            "triggers": {"all": o.get("all", []), "any": o.get("any", [])},
            "urgency": o["u"],
            "timing": o["tm"],
            "where": where,
            "documents": docs,
            "limits": limits,
            "notify_warning": o.get("nw"),
            "status": status,
            "pending_effective": o.get("pe") if status == "pending" else None,
            "confidence": conf(cf),
            "plain": plain,
            "plain_reviewed": reviewed,
        })
    missing = set(OV) - set(rows)
    if missing:
        raise SystemExit(f"OV ids not in research tables: {sorted(missing)}")
    out = {"version": VERSION,
           "flags": [{"id": i, "label": l, "group": g} for i, l, g in FLAGS],
           "items": items}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} items={len(items)} flags={len(FLAGS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
