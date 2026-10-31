"""2026-11-01 보류 세트 실패 유형에 대한 결정적 검사(한계 표현 보존·보호 효과 단정·제도 이름 혼동·처벌·의무).

모든 행·문장은 합성 시험용이다(실제 사건 아님). LLM 은 부르지 않는다.
"""
import pytest

import llm
import verify

J8 = {"id": "J8", "measure": "부본의 주거·주소·연락처를 가리고 송달(재량)",
      "plain": "보호명령 청구서 부본을 보낼 때 주소를 가리는 제도예요. 청구서에 가림 요청을 적어 쓸 수 있지만, "
               "법원 재량이라 허가 열람 등 다른 서류에서 노출될 수 있어요.",
      "limits": "재량. 판사 허가 열람으로 다른 서류에서 노출 가능"}
J7 = {"id": "J7", "measure": "가사소송은 개인정보 보호조치 준용, 송달장소 신고",
      "plain": "이혼 등 가사소송에서 개인정보 보호조치를 준용해 주소 등을 가려요. 가사비송에는 적용이 확인되지 않았어요.",
      "limits": "가사비송은 비송사건절차법 준용이라 적용 미확인"}
L7 = {"id": "L7", "measure": "명의도용방지·가입사실조회·가입제한 서비스",
      "plain": "가입제한 서비스로 내 명의 신규 개통을 막을 수 있어요. 명의도용방지 서비스는 개통 사실을 문자로 알려 줘요.",
      "where": "엠세이퍼(KAIT), 통신사 매장"}
J12 = {"id": "J12", "measure": "퇴거, 접근금지, 임시보호명령",
       "plain": "가정폭력 피해자가 법원에 피해자보호명령을 청구하는 제도예요.", "where": "가정법원"}
J13 = {"id": "J13", "measure": "임시조치(법원 결정)", "plain": "법원이 임시조치로 접근금지를 정해요.", "where": "법원"}
P6 = {"id": "P6", "measure": "삭제지원(비용은 가해자 부담)", "plain": "촬영물과 함께 퍼진 이름·학교를 삭제지원해요.",
      "limits": "불법촬영물 피해로 한정"}
J2 = {"id": "J2", "measure": "송달장소·송달영수인 신고", "documents": "신고서, 송달영수인 동의",
      "plain": "법원에 송달받을 장소와 송달영수인을 신고하는 제도예요."}


def bad(sentence, items):
    return verify.rule_check(sentence, verify.build_ctx(items))


# ---------------------------------------------------------------- 한계 표현 보존
def test_hedge_unconfirmed_turned_into_negation_fails():
    r = bad("다만, 가사비송에는 해당 제도가 적용되지 않습니다. [J7]", [J7])
    assert any("한계 표현 누락" in x for x in r)


def test_hedge_unconfirmed_preserved_passes():
    assert bad("가사비송에는 적용이 확인되지 않았어요. [J7]", [J7]) == []


def test_hedge_discretion_exposure_denial_fails():
    r = bad("가림을 요청하면 주소가 드러나지 않아요. [J8]", [J8])
    assert any("보호 효과 단정" in x for x in r)


def test_hedge_discretion_kept_passes():
    s = "가림을 요청할 수 있지만 법원 재량이라 다른 서류에서 노출될 수 있어요. [J8]"
    assert bad(s, [J8]) == []


def test_unrelated_sentence_not_flagged_by_hedge_rule():
    # 한계 표현이 있는 절과 낱말이 겹치지 않는 문장은 이 규칙으로 걸리지 않는다.
    assert bad("가사소송에서 개인정보 보호조치를 준용해요. [J7]", [J7]) == []


# ---------------------------------------------------------------- 제도 이름·기능 혼동
def test_identity_theft_service_does_not_block():
    r = bad("명의도용방지서비스로 내 명의 개통을 막을 수 있어요. [L7]", [L7])
    assert any("기능 혼동" in x for x in r)


def test_subscription_restriction_blocks_ok():
    assert not any("기능 혼동" in x for x in bad("가입제한 서비스로 내 명의 신규 개통을 막을 수 있어요. [L7]", [L7]))


def test_similar_measure_name_not_in_rows_fails():
    r = bad("경찰이 긴급임시조치를 할 수 있어요. [J13]", [J13])
    assert any("긴급임시조치" in x for x in r)
    r2 = bad("임시보호명령은 가정법원에 청구해요. [J13]", [J13])
    assert any("임시보호명령" in x for x in r2)


def test_exact_measure_name_in_rows_passes():
    assert not any("제도 이름" in x for x in bad("법원이 임시조치로 접근금지를 정해요. [J13]", [J13]))


# ---------------------------------------------------------------- 근거 없는 처벌·의무·제한·기관
def test_penalty_claim_without_penalty_text_fails():
    r = bad("보호명령을 어기면 처벌받을 수 있어요. [J12]", [J12])
    assert any("처벌" in x for x in r)


def test_penalty_unconfirmed_statement_passes():
    # 답변 지시문은 근거에 없는 처벌 여부를 '근거에서 확인되지 않습니다'로 쓰라고 한다. 그 문장을 처벌 주장으로 막으면
    # 지시를 따른 답이 재생성·발췌로 밀려난다.
    assert bad("처벌되는지는 근거에서 확인되지 않습니다. [J12]", [J12]) == []
    assert bad("어기면 처벌받는지는 근거에 나와 있지 않아요. [J12]", [J12]) == []


def test_penalty_claim_next_to_unconfirmed_clause_still_fails():
    # 다른 절의 '확인되지 않'으로 처벌 단정이 풀리면 안 된다.
    r = bad("어기면 처벌받습니다. 기간은 근거에서 확인되지 않습니다. [J12]", [J12])
    assert any("처벌" in x for x in r)


def test_obligation_not_in_evidence_fails():
    r = bad("신고할 때 송달영수인도 함께 정해야 합니다. [J2]", [J2])
    assert any("의무 단정" in x for x in r)


def test_restriction_hanhae_not_in_evidence_fails():
    r = bad("삭제지원은 이름·학교 같은 신상정보에 한해 가능합니다. [P6]", [P6])
    assert any("단정" in x for x in r)


def test_new_agency_not_in_evidence_fails():
    r = bad("방송통신심의위원회에 신고하면 삭제지원을 받을 수 있어요. [P6]", [P6])
    assert any("방송통신심의위원회" in x for x in r)


# ---------------------------------------------------------------- 검증 루프와 결합
def test_hedge_violation_triggers_regeneration_then_passes():
    from test_verify import Clock, router

    first = {"answerable": True, "answer": "가림을 요청하면 주소가 드러나지 않아요. [J8]", "cite": ["J8"]}
    second = {"answerable": True,
              "answer": "가림을 요청할 수 있지만 법원 재량이라 다른 서류에서 노출될 수 있어요. [J8]", "cite": ["J8"]}
    seen = []
    deps = verify.Deps(chat_json=router([first, second], seen=seen), now=Clock())
    out = verify.run_answer("결정문에 주소가 적히나요?", [J8], deps, budget_s=100)
    assert out["mode"] == "regenerated"
    fb = [m for m in seen if "results" not in m["schema"]["properties"]][1]["messages"][0]["content"]
    assert "재작성 지시" in fb and "재량" in fb


def test_hedge_violation_twice_falls_back_to_extractive():
    from test_verify import Clock, router

    a = {"answerable": True, "answer": "가사비송에도 적용되지 않습니다. [J7]", "cite": ["J7"]}
    deps = verify.Deps(chat_json=router([a, a]), now=Clock())
    out = verify.run_answer("가사비송에도 되나요?", [J7], deps, budget_s=100)
    assert out["mode"] == "extractive" and out["ai_generated"] is False


@pytest.mark.parametrize("text", [
    "근거에서 확인되지 않습니다. 1366(24시간)·법률구조공단 132에 문의하세요.",
])
def test_advice_and_not_found_phrases_not_flagged(text):
    assert bad(text, [J8, J7, L7]) == []


def test_prompt_has_hedge_and_measure_rules():
    sys_ = llm.answer_messages("q", [J8])[0]["content"]
    assert "재량" in sys_ and "한 문장에는 제도 하나만" in sys_ and "가입제한서비스" in sys_
    assert "한계 표현" in verify.VERIFY_SYS


def test_penalty_in_law_name_only_does_not_count():
    it = dict(J12, articles=[{"law": "가정폭력범죄의 처벌 등에 관한 특례법", "article": "제55조의2",
                              "text": "① 판사는 피해자보호명령을 할 수 있다."}])
    assert any("처벌" in x for x in bad("보호명령을 어기면 처벌이 되는 경우가 있습니다. [J12]", [it]))
    it2 = dict(it, articles=[{"law": "가정폭력범죄의 처벌 등에 관한 특례법", "article": "제63조",
                              "text": "피해자보호명령을 이행하지 아니한 자는 2년 이하의 징역에 처한다."}])
    assert not any("처벌" in x for x in bad("보호명령을 어기면 처벌받을 수 있어요. [J12]", [it2]))


def test_obligation_supported_by_same_verb_in_article_passes():
    it = dict(J2, articles=[{"law": "민사소송법", "article": "제185조",
                             "text": "① 송달받을 장소를 바꿀 때에는 바로 그 취지를 법원에 신고하여야 한다."}])
    assert not any("의무" in x for x in bad("송달장소를 바꾸면 법원에 신고해야 합니다. [J2]", [it]))


def test_unrelated_unconfirmed_note_does_not_block_protective_claim():
    # '미확인'은 적용 여부 한계라 보호 효과 단정 검사 대상이 아니다(재량·노출 가능만).
    it = dict(J7, articles=[{"law": "가사소송법", "article": "제10조",
                             "text": "가정법원에서 처리 중인 사건은 본인임을 알 수 있는 사실을 게재할 수 없다."}])
    assert not any("보호 효과" in x for x in bad("가정법원 사건은 본인을 알아볼 수 있는 내용의 게재를 막습니다. [J7]", [it]))
