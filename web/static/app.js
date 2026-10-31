/*
 * 숨은집 지킴이 프런트엔드.
 * 원칙:
 *  - 모든 동적 텍스트는 textContent/createElement 로만 삽입한다(innerHTML 사용 금지).
 *  - localStorage/sessionStorage/cookie 사용 금지.
 *  - API 실패 시 사용자에게 보이는 메시지를 남긴다.
 */
(function () {
  'use strict';

  var FALLBACK_FLAGS = [
    { id: 'moved_recently', label: '최근 이사(또는 이사 예정)', group: '주거' },
    { id: 'same_registration_address', label: '가해자와 주민등록지가 같음', group: '주거' },
    { id: 'lawsuit', label: '민사·가사 소송 중/예정', group: '소송·수사' },
    { id: 'abuser_is_plaintiff', label: '가해자가 원고(소송을 건 쪽)', group: '소송·수사' },
    { id: 'criminal_case', label: '고소·수사 중', group: '소송·수사' },
    { id: 'protection_order', label: '접근금지 등 조치 있음/예정', group: '소송·수사' },
    { id: 'children_school', label: '자녀가 학교에 다님', group: '자녀' },
    { id: 'children_daycare', label: '자녀가 어린이집·유치원에 다님', group: '자녀' },
    { id: 'vehicle', label: '차량 소유', group: '차량·우편·통신' },
    { id: 'shared_vehicle_or_abuser_owned', label: '가해자와 공동 소유/가해자 명의 차량', group: '차량·우편·통신' },
    { id: 'own_property_or_lease_registration', label: '내 부동산·임차 등기 있음', group: '차량·우편·통신' },
    { id: 'company_officer', label: '법인 임원 등재', group: '차량·우편·통신' },
    { id: 'mail_old_address', label: '옛 주소로 우편 수신', group: '차량·우편·통신' },
    { id: 'parcel_delivery', label: '택배 이용', group: '차량·우편·통신' },
    { id: 'abuser_named_phone_or_family_plan', label: '가해자 명의·가족 요금제 휴대전화', group: '차량·우편·통신' },
    { id: 'shared_accounts_or_location_sharing', label: '공유 계정·위치 공유', group: '디지털' },
    { id: 'bluetooth_tag_suspected', label: '블루투스 추적 태그 의심', group: '디지털' },
    { id: 'photos_social', label: '사진·SNS 게시', group: '디지털' },
    { id: 'foreign_national', label: '외국인', group: '기타' },
    { id: 'health_insurance_dependent', label: '건강보험 피부양자', group: '기타' },
    { id: 'shelter_needed', label: '보호시설 필요', group: '기타' },
    { id: 'workplace_known', label: '직장이 알려짐', group: '기타' },
    { id: 'election_period', label: '선거 시기', group: '기타' }
  ];

  var VICTIM_LABELS = {
    family: '가정폭력',
    stalking: '스토킹',
    dating: '교제폭력',
    sexual: '성폭력',
    any: '유형 무관'
  };

  var URGENCY_LABELS = { 1: '지금 먼저', 2: '빨리', 3: '여유' };

  var GROUP_ORDER = ['주거', '소송·수사', '자녀', '차량·우편·통신', '디지털', '기타'];

  var tier1Spans = [];

  function $(id) { return document.getElementById(id); }

  function setStatus(id, msg) {
    var el = $(id);
    if (el) el.textContent = msg || '';
  }

  function safeUrl(u) {
    if (typeof u !== 'string' || !u) return null;
    if (/^https?:\/\//i.test(u)) return u;
    if (u.charAt(0) === '/' && u.charAt(1) !== '/') return u;
    return null;
  }

  /* ---------- flags ---------- */
  function loadFlags() {
    return fetch('/api/flags', { headers: { 'Accept': 'application/json' } })
      .then(function (res) {
        if (!res.ok) throw new Error('flags http ' + res.status);
        return res.json();
      })
      .then(function (data) {
        var arr = null;
        if (Array.isArray(data)) arr = data;
        else if (data && Array.isArray(data.flags)) arr = data.flags;
        if (!arr || !arr.length) throw new Error('flags empty');
        return arr;
      })
      .catch(function () {
        return FALLBACK_FLAGS.slice();
      });
  }

  function renderFlags(flags) {
    var container = $('flags-container');
    container.textContent = '';

    var groups = {};
    for (var i = 0; i < flags.length; i++) {
      var f = flags[i];
      var g = (f && f.group) ? f.group : '기타';
      if (!groups[g]) groups[g] = [];
      groups[g].push(f);
    }
    var order = [];
    for (var j = 0; j < GROUP_ORDER.length; j++) {
      if (groups[GROUP_ORDER[j]]) order.push(GROUP_ORDER[j]);
    }
    for (var gk in groups) {
      if (Object.prototype.hasOwnProperty.call(groups, gk) && order.indexOf(gk) === -1) order.push(gk);
    }

    for (var oi = 0; oi < order.length; oi++) {
      var gname = order[oi];
      var fieldset = document.createElement('fieldset');
      fieldset.className = 'flag-group';
      var legend = document.createElement('legend');
      legend.textContent = gname;
      fieldset.appendChild(legend);

      var list = groups[gname];
      for (var li = 0; li < list.length; li++) {
        var item = list[li];
        var lbl = document.createElement('label');
        lbl.className = 'flag-item';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.name = 'flags';
        cb.value = item.id;
        var span = document.createElement('span');
        span.textContent = item.label || item.id;
        lbl.appendChild(cb);
        lbl.appendChild(span);
        fieldset.appendChild(lbl);
      }
      container.appendChild(fieldset);
    }
  }

  /* ---------- diagnose ---------- */
  function onDiagnoseSubmit(ev) {
    ev.preventDefault();
    var form = ev.currentTarget;
    var victim = form.querySelector('input[name="victim_type"]:checked');
    if (!victim) {
      setStatus('diagnose-status', '피해 유형을 선택하세요.');
      return;
    }
    var flags = [];
    var checked = form.querySelectorAll('input[name="flags"]:checked');
    for (var i = 0; i < checked.length; i++) flags.push(checked[i].value);

    setStatus('diagnose-status', '진단 중...');
    $('diagnose-results').textContent = '';

    fetch('/api/diagnose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ victim_type: victim.value, flags: flags })
    })
      .then(function (res) {
        if (!res.ok) throw new Error('diagnose http ' + res.status);
        return res.json();
      })
      .then(function (data) {
        setStatus('diagnose-status', '');
        renderDiagnose(data || {});
      })
      .catch(function () {
        setStatus('diagnose-status', '진단을 불러오지 못했습니다. 잠시 후 다시 시도하세요.');
      });
  }

  function badge(cls, text) {
    var b = document.createElement('span');
    b.className = 'badge ' + cls;
    b.textContent = text;
    return b;
  }

  function row(label, value) {
    var p = document.createElement('p');
    p.className = 'card-row';
    var l = document.createElement('span');
    l.className = 'row-label';
    l.textContent = label;
    p.appendChild(l);
    p.appendChild(document.createTextNode(value));
    return p;
  }

  function buildCard(item) {
    var card = document.createElement('article');
    card.className = 'card';
    if (item.status === 'pending') card.classList.add('card-pending');

    var head = document.createElement('div');
    head.className = 'card-head';

    var u = item.urgency;
    if (u === 1 || u === 2 || u === 3) head.appendChild(badge('urgency-' + u, URGENCY_LABELS[u]));
    if (item.pending_effective) head.appendChild(badge('pending', '시행 예정 ' + item.pending_effective));

    var title = document.createElement('h3');
    title.className = 'card-title';
    title.textContent = item.measure || item.id || '';
    head.appendChild(title);
    card.appendChild(head);

    if (item.note) {
      var note = document.createElement('p');
      note.className = 'card-note';
      note.textContent = item.note;
      card.appendChild(note);
    }

    // confidence + victim_types meta row
    var hasMeta = (item.confidence || (Array.isArray(item.victim_types) && item.victim_types.length));
    if (hasMeta) {
      var meta = document.createElement('div');
      meta.className = 'card-meta';
      if (item.confidence) {
        meta.appendChild(badge('confidence conf-' + item.confidence, item.confidence));
      }
      if (Array.isArray(item.victim_types) && item.victim_types.length) {
        for (var vi = 0; vi < item.victim_types.length; vi++) {
          var vt = item.victim_types[vi];
          var chip = document.createElement('span');
          chip.className = 'chip victim';
          chip.textContent = VICTIM_LABELS[vt] || vt;
          meta.appendChild(chip);
        }
      }
      card.appendChild(meta);
    }

    if (item.notify_warning) {
      var warn = document.createElement('div');
      warn.className = 'warn-box';
      var lead = document.createElement('strong');
      lead.textContent = '⚠ 신청 사실이 상대에게 알려질 수 있습니다';
      warn.appendChild(lead);
      var body = document.createElement('p');
      body.textContent = item.notify_warning;
      warn.appendChild(body);
      card.appendChild(warn);
    }

    if (item.plain) {
      var plain = document.createElement('div');
      plain.className = 'plain-box';
      if (!item.plain_reviewed) {
        plain.appendChild(badge('ai-badge', 'AI 생성(검수 전)'));
      }
      var pt = document.createElement('p');
      pt.textContent = item.plain;
      plain.appendChild(pt);
      card.appendChild(plain);
    }

    if (item.path) card.appendChild(row('경로', item.path));
    if (item.exposure) card.appendChild(row('노출', item.exposure));
    if (item.timing) card.appendChild(row('시기', item.timing));

    if (Array.isArray(item.basis) && item.basis.length) {
      var chips = document.createElement('div');
      chips.className = 'chips';
      for (var bi = 0; bi < item.basis.length; bi++) {
        var b = item.basis[bi];
        var txt = ((b.law || '') + ' ' + (b.article || '')).trim();
        if (!txt) txt = '근거';
        if (b.effective) txt += ' (시행 ' + b.effective + ')';

        var url = safeUrl(b.url);
        var chipEl;
        if (url) {
          chipEl = document.createElement('a');
          chipEl.href = url;
          chipEl.target = '_blank';
          chipEl.rel = 'noopener noreferrer';
        } else {
          chipEl = document.createElement('span');
        }
        chipEl.className = 'chip';
        chipEl.textContent = txt;
        chips.appendChild(chipEl);
      }
      card.appendChild(chips);
    }

    var dets = [
      ['신청처', item.where],
      ['서류', item.documents],
      ['한계', item.limits]
    ];
    for (var di = 0; di < dets.length; di++) {
      var lab = dets[di][0];
      var val = dets[di][1];
      if (!val) continue;
      var det = document.createElement('details');
      var sum = document.createElement('summary');
      sum.textContent = lab;
      det.appendChild(sum);
      var p = document.createElement('p');
      p.textContent = val;
      det.appendChild(p);
      card.appendChild(det);
    }

    return card;
  }

  function renderDiagnose(data) {
    var root = $('diagnose-results');
    root.textContent = '';

    if (data.summary && typeof data.summary === 'object') {
      var parts = [];
      if (typeof data.summary.total === 'number') parts.push('해당 제도 ' + data.summary.total + '건');
      else if (typeof data.summary.checklist === 'number') parts.push('해당 제도 ' + data.summary.checklist + '건');
      if (typeof data.summary.residual === 'number') parts.push('잔여 위험 ' + data.summary.residual + '건');
      if (typeof data.summary.not_applicable === 'number') parts.push('해당 없음 ' + data.summary.not_applicable + '건');
      if (parts.length) {
        var sumBox = document.createElement('div');
        sumBox.className = 'summary-box';
        sumBox.textContent = parts.join(' · ');
        root.appendChild(sumBox);
      }
    }

    var printBtn = document.createElement('button');
    printBtn.type = 'button';
    printBtn.className = 'print-btn';
    printBtn.textContent = '결과 인쇄';
    printBtn.addEventListener('click', function () { window.print(); });
    root.appendChild(printBtn);

    var checklist = Array.isArray(data.checklist) ? data.checklist : [];
    if (checklist.length) {
      var h1 = document.createElement('h2');
      h1.textContent = '급한 순 체크리스트';
      root.appendChild(h1);
      for (var i = 0; i < checklist.length; i++) root.appendChild(buildCard(checklist[i]));
    } else {
      var empty = document.createElement('p');
      empty.textContent = '해당하는 제도가 없습니다. 상황 체크를 바꾸어 다시 시도해 보세요.';
      root.appendChild(empty);
    }

    var residual = Array.isArray(data.residual) ? data.residual : [];
    if (residual.length) {
      var h2 = document.createElement('h2');
      h2.textContent = '막을 제도가 없는 경로(잔여 위험)';
      root.appendChild(h2);
      for (var r = 0; r < residual.length; r++) {
        var it = residual[r] || {};
        var card = document.createElement('article');
        card.className = 'card residual';
        var t = document.createElement('h3');
        t.className = 'card-title';
        t.textContent = it.measure || it.path || it.id || '잔여 위험';
        card.appendChild(t);
        if (it.plain) {
          var pbox = document.createElement('div');
          pbox.className = 'plain-box';
          if (!it.plain_reviewed) {
            pbox.appendChild(badge('ai-badge', 'AI 생성(검수 전)'));
          }
          var pp = document.createElement('p');
          pp.textContent = it.plain;
          pbox.appendChild(pp);
          card.appendChild(pbox);
        }
        if (it.timing) card.appendChild(row('대비', it.timing));
        if (it.path) card.appendChild(row('경로', it.path));
        if (it.support) {
          var sn = document.createElement('p');
          sn.className = 'card-note';
          sn.textContent = it.support;
          card.appendChild(sn);
        }
        root.appendChild(card);
      }
    }

    var na = Array.isArray(data.not_applicable) ? data.not_applicable : [];
    if (na.length) {
      var h3 = document.createElement('h2');
      h3.textContent = '이 피해 유형에는 해당 제도가 없습니다';
      root.appendChild(h3);
      var ul = document.createElement('ul');
      ul.className = 'na-list';
      for (var k = 0; k < na.length; k++) {
        var n = na[k] || {};
        var li = document.createElement('li');
        var strong = document.createElement('strong');
        strong.textContent = n.measure || n.id || '';
        li.appendChild(strong);
        if (n.reason) {
          li.appendChild(document.createTextNode(' — ' + n.reason));
        }
        ul.appendChild(li);
      }
      root.appendChild(ul);
    }
  }

  /* ---------- document check ---------- */
  function renderHighlights(text, spans) {
    var hl = $('doc-highlight');
    hl.textContent = '';
    var sorted = spans.slice().sort(function (a, b) {
      if (a.start !== b.start) return a.start - b.start;
      return a.end - b.end;
    });
    var pos = 0;
    for (var i = 0; i < sorted.length; i++) {
      var s = sorted[i];
      if (s.start < pos) continue;
      if (s.start > pos) hl.appendChild(document.createTextNode(text.slice(pos, s.start)));
      var mark = document.createElement('mark');
      mark.setAttribute('data-label', s.label || '');
      mark.title = (s.label || '') + (s.suggestion ? ' — ' + s.suggestion : '');
      if (s.tier === 2) mark.classList.add('tier2');
      mark.textContent = text.slice(s.start, s.end);
      hl.appendChild(mark);
      pos = s.end;
    }
    if (pos < text.length) hl.appendChild(document.createTextNode(text.slice(pos)));
  }

  function renderFindings(spans) {
    var root = $('doc-findings');
    root.textContent = '';
    if (!spans.length) {
      var p0 = document.createElement('p');
      p0.textContent = '발견된 항목이 없습니다.';
      root.appendChild(p0);
      return;
    }
    var h = document.createElement('h3');
    h.textContent = '발견 항목 (' + spans.length + '건)';
    root.appendChild(h);
    var ul = document.createElement('ul');
    ul.className = 'findings';
    for (var i = 0; i < spans.length; i++) {
      var s = spans[i];
      var li = document.createElement('li');
      var b = document.createElement('strong');
      b.textContent = s.label || '';
      li.appendChild(b);
      li.appendChild(document.createTextNode(' 「' + (s.text || '') + '」'));
      if (s.tier) {
        var tb = document.createElement('span');
        tb.className = 'tier-badge';
        tb.textContent = s.tier + '단계';
        li.appendChild(tb);
      }
      if (s.suggestion) {
        var sg = document.createElement('div');
        sg.className = 'suggestion';
        sg.textContent = '→ ' + s.suggestion;
        li.appendChild(sg);
      }
      ul.appendChild(li);
    }
    root.appendChild(ul);
  }

  function renderDocResults(text, spans) {
    renderHighlights(text, spans);
    renderFindings(spans);
  }

  function runTier1() {
    var text = $('doc-text').value;
    var spans = (typeof window.detectSpans === 'function') ? window.detectSpans(text) : [];
    tier1Spans = spans.map(function (s) {
      return { start: s.start, end: s.end, label: s.label, text: s.text, suggestion: s.suggestion, tier: 1 };
    });
    renderDocResults(text, tier1Spans);
    setStatus('doc-status', '1단계 점검 완료 (' + tier1Spans.length + '건)');
  }

  function mergeSpans(a, b) {
    var all = a.concat(b).sort(function (x, y) {
      var lx = x.end - x.start;
      var ly = y.end - y.start;
      if (ly !== lx) return ly - lx;
      if (x.start !== y.start) return x.start - y.start;
      return (x.tier || 2) - (y.tier || 2);
    });
    var out = [];
    for (var i = 0; i < all.length; i++) {
      var c = all[i];
      var overlap = false;
      for (var j = 0; j < out.length; j++) {
        var s = out[j];
        if (c.start < s.end && s.start < c.end) { overlap = true; break; }
      }
      if (!overlap) out.push(c);
    }
    out.sort(function (x, y) { return x.start - y.start; });
    return out;
  }

  function runTier2() {
    var text = $('doc-text').value;
    if (!text || !text.trim()) {
      setStatus('doc-status', '내용을 입력하세요.');
      return;
    }
    setStatus('doc-status', '2단계 점검 요청 중...');
    fetch('/api/doccheck', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text })
    })
      .then(function (res) {
        if (!res.ok) throw new Error('doccheck http ' + res.status);
        return res.json();
      })
      .then(function (data) {
        if (!data || data.available === false) {
          setStatus('doc-status', '2단계 점검을 지금 쓸 수 없습니다');
          return;
        }
        var raw = Array.isArray(data.spans) ? data.spans : [];
        var t2 = [];
        for (var i = 0; i < raw.length; i++) {
          var s = raw[i] || {};
          if (typeof s.start !== 'number' || typeof s.end !== 'number') continue;
          if (s.end <= s.start) continue;
          t2.push({
            start: s.start,
            end: s.end,
            label: s.label || '기타',
            text: typeof s.text === 'string' ? s.text : text.slice(s.start, s.end),
            suggestion: null,
            tier: 2
          });
        }
        var merged = mergeSpans(tier1Spans, t2);
        renderDocResults(text, merged);
        setStatus('doc-status', '2단계 점검 완료 (' + merged.length + '건)');
      })
      .catch(function () {
        setStatus('doc-status', '2단계 점검을 지금 쓸 수 없습니다');
      });
  }

  /* ---------- search ---------- */
  function runSearch() {
    var q = $('search-q').value.trim();
    var root = $('search-results');
    root.textContent = '';
    if (!q) {
      setStatus('search-status', '질문을 입력하세요.');
      return;
    }
    setStatus('search-status', '검색 중...');
    fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ q: q })
    })
      .then(function (res) {
        if (!res.ok) throw new Error('search http ' + res.status);
        return res.json();
      })
      .then(function (data) {
        if (data && data.refused) {
          setStatus('search-status', data.message || '이 질문에는 답변을 제공할 수 없습니다.');
          return;
        }
        setStatus('search-status', '');
        if (data && data.fallback) {
          var notice = document.createElement('p');
          notice.className = 'notice';
          notice.textContent = '키워드 검색으로 대체되었습니다';
          root.appendChild(notice);
        }
        var list = [];
        if (Array.isArray(data)) list = data;
        else if (data && Array.isArray(data.results)) list = data.results;

        if (!list.length) {
          var p = document.createElement('p');
          p.textContent = '결과가 없습니다.';
          root.appendChild(p);
          return;
        }
        for (var i = 0; i < list.length; i++) {
          var r = list[i] || {};
          var card = document.createElement('article');
          card.className = 'card';
          var h = document.createElement('h3');
          h.className = 'card-title';
          var titleText = ((r.law || '') + ' ' + (r.article || '')).trim() || '근거';
          var url = safeUrl(r.url);
          if (url) {
            var a = document.createElement('a');
            a.href = url;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            a.textContent = titleText;
            h.appendChild(a);
          } else {
            h.textContent = titleText;
          }
          card.appendChild(h);
          if (r.effective) {
            var meta = document.createElement('p');
            meta.className = 'meta';
            meta.textContent = '시행 ' + r.effective;
            card.appendChild(meta);
          }
          if (r.text) {
            var body = document.createElement('p');
            body.textContent = r.text;
            card.appendChild(body);
          }
          root.appendChild(card);
        }
      })
      .catch(function () {
        setStatus('search-status', '검색에 실패했습니다. 잠시 후 다시 시도하세요.');
      });
  }

  /* ---------- tabs ---------- */
  function activateTab(tab) {
    var tabs = document.querySelectorAll('[role="tab"]');
    for (var i = 0; i < tabs.length; i++) {
      var t = tabs[i];
      var sel = (t === tab);
      t.setAttribute('aria-selected', sel ? 'true' : 'false');
      t.setAttribute('tabindex', sel ? '0' : '-1');
    }
    var panels = document.querySelectorAll('[role="tabpanel"]');
    for (var j = 0; j < panels.length; j++) panels[j].hidden = true;
    var panelId = tab.getAttribute('aria-controls');
    if (panelId) {
      var panel = document.getElementById(panelId);
      if (panel) panel.hidden = false;
    }
  }

  function initTabs() {
    var tabs = document.querySelectorAll('[role="tab"]');
    for (var i = 0; i < tabs.length; i++) {
      (function (tab) {
        tab.addEventListener('click', function () { activateTab(tab); });
        tab.addEventListener('keydown', function (e) {
          if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'Home' && e.key !== 'End') return;
          e.preventDefault();
          var arr = Array.prototype.slice.call(tabs);
          var idx = arr.indexOf(tab);
          var next;
          if (e.key === 'Home') next = arr[0];
          else if (e.key === 'End') next = arr[arr.length - 1];
          else if (e.key === 'ArrowRight') next = arr[(idx + 1) % arr.length];
          else next = arr[(idx - 1 + arr.length) % arr.length];
          next.focus();
          activateTab(next);
        });
      })(tabs[i]);
    }
  }

  /* ---------- quick exit ---------- */
  function quickExit() {
    var target = 'https://www.google.com/search?q=날씨';
    try {
      var fields = document.querySelectorAll('input, textarea');
      for (var i = 0; i < fields.length; i++) {
        var el = fields[i];
        if (el.type === 'checkbox' || el.type === 'radio') el.checked = false;
        else el.value = '';
      }
      document.body.textContent = '';
    } catch (e) { /* noop */ }
    location.replace(target);
  }

  /* ---------- init ---------- */
  function init() {
    initTabs();

    loadFlags().then(renderFlags);

    $('diagnose-form').addEventListener('submit', onDiagnoseSubmit);
    $('doc-tier1').addEventListener('click', runTier1);
    $('doc-tier2').addEventListener('click', runTier2);
    $('search-btn').addEventListener('click', runSearch);
    $('search-q').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        runSearch();
      }
    });
    $('quick-exit').addEventListener('click', quickExit);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
