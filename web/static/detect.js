/*
 * 1단계 서류 점검: 브라우저에서만 동작하는 정규식 기반 스팬 탐지.
 * 순수 함수. 부작용 없음. 정규식은 매 호출 새로 만든다.
 */
(function () {
  'use strict';

  function suggestFor(label, raw) {
    switch (label) {
      case '학교': {
        var who = '자녀(학생)';
        if (raw.indexOf('초등학교') !== -1) who = '자녀(초등학생)';
        else if (raw.indexOf('중학교') !== -1) who = '자녀(중학생)';
        else if (raw.indexOf('고등학교') !== -1) who = '자녀(고등학생)';
        else if (raw.indexOf('어린이집') !== -1 || raw.indexOf('유치원') !== -1) who = '자녀(영유아)';
        return who + ' 등 비식별 표현으로 대체';
      }
      case '직장':
        return '원고의 직장 등 비식별 표현으로 대체';
      case '주소':
        return '주소 비공개 신청과 함께 기재(주소 대신 송달 가능한 비공개 방식 사용)';
      case '전화':
        return '연락처 비공개 처리 또는 가림';
      case '주민번호':
        return '앞 6자리 이외 가림 또는 삭제';
      case '차량':
        return '차량번호 가림';
      case '이메일':
        return '이메일 가림';
      default:
        return '비식별 처리';
    }
  }

  // 우선순위: 배열 인덱스가 작을수록 우선
  function buildPatterns() {
    return [
      ['주민번호', /\d{6}-?[1-4]\d{6}/g],
      ['전화', /(01[016789]|0\d{1,2})[-.\s]?\d{3,4}[-.\s]?\d{4}/g],
      ['이메일', /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g],
      ['차량', /\d{2,3}[가-힣]\s?\d{4}/g],
      ['주소', /(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)[가-힣]*(특별시|광역시|특별자치시|도|특별자치도)?\s?[가-힣]+(시|군|구)\s?[가-힣0-9]+(로|길|동|읍|면)(\s?\d+(-\d+)?)?(\s?\d+동)?(\s?\d+호)?/g],
      ['주소', /[가-힣]+(구|시|군)\s[가-힣0-9]+(로|길|동)\s?\d+(-\d+)?(번지)?/g],
      ['주소', /\d+동\s?\d+호/g],
      ['학교', /[가-힣]+(초등학교|중학교|고등학교|어린이집|유치원)(\s?\d+학년)?(\s?\d+반)?/g],
      ['직장', /[가-힣A-Za-z0-9]+(주식회사|회사)/g],
      ['직장', /㈜\s?[가-힣A-Za-z0-9]+/g],
      ['직장', /주식회사\s?[가-힣A-Za-z0-9]+/g],
      ['직장', /[가-힣]{1,10}팀/g],
      ['직장', /[가-힣A-Za-z0-9]+에서\s?근무/g]
    ];
  }

  function detectSpans(text) {
    if (typeof text !== 'string' || text.length === 0) return [];

    var patterns = buildPatterns();
    var candidates = [];

    for (var p = 0; p < patterns.length; p++) {
      var label = patterns[p][0];
      var re = patterns[p][1];
      var m;
      while ((m = re.exec(text)) !== null) {
        var start = m.index;
        var end = start + m[0].length;
        if (end > start) {
          candidates.push({
            start: start,
            end: end,
            label: label,
            text: m[0],
            priority: p
          });
        }
        if (m.index === re.lastIndex) re.lastIndex++;
      }
    }

    // 긴 것 우선, 같으면 앞선 것, 같으면 정의 우선순위
    candidates.sort(function (a, b) {
      var la = a.end - a.start;
      var lb = b.end - b.start;
      if (lb !== la) return lb - la;
      if (a.start !== b.start) return a.start - b.start;
      return a.priority - b.priority;
    });

    // 겹침 해소: 그리디 선택
    var selected = [];
    for (var i = 0; i < candidates.length; i++) {
      var c = candidates[i];
      var overlap = false;
      for (var j = 0; j < selected.length; j++) {
        var s = selected[j];
        if (c.start < s.end && s.start < c.end) { overlap = true; break; }
      }
      if (!overlap) selected.push(c);
    }

    selected.sort(function (a, b) { return a.start - b.start; });

    var out = [];
    for (var k = 0; k < selected.length; k++) {
      var sp = selected[k];
      out.push({
        start: sp.start,
        end: sp.end,
        label: sp.label,
        text: sp.text,
        suggestion: suggestFor(sp.label, sp.text)
      });
    }
    return out;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { detectSpans: detectSpans };
  }
  if (typeof globalThis !== 'undefined') {
    globalThis.detectSpans = detectSpans;
  }
  if (typeof window !== 'undefined') {
    window.detectSpans = detectSpans;
  }
})();
