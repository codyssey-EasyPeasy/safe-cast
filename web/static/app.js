/*
 * 숨은집 지킴이 프런트엔드.
 * 원칙:
 *  - 모든 동적 텍스트는 textContent/createElement 로만 삽입한다(innerHTML 사용 금지).
 *  - localStorage/sessionStorage/cookie 사용 금지.
 *  - API 실패 시 사용자에게 보이는 메시지를 남긴다.
 *  - 내부 LLM 기능(말로 설명하기·이게 무슨 뜻인가요?·안전한 문장으로 바꾸기)은
 *    GET /api/features 의 llm=true 일 때만 보인다. 실패(503)하면 즉시 숨기고
 *    체크박스 방식으로 그대로 쓴다. LLM 결과는 자동 제출하지 않는다.
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
  var lastSpans = [];
  var cardSeq = 0;
  var lastText = '';
  var llmOn = false;
  var LLM_DOWN_MSG = '지금은 이 기능을 쓸 수 없습니다. 체크 항목으로 계속 진행할 수 있습니다.';
  var REWRITE_MAX = 1500; // app/main.py REWRITE_MAX_CHARS 와 같게
  var intakePending = false; // '말로 설명하기'로 미리 고른 뒤 사용자가 확인하기 전까지 true

  function $(id) { return document.getElementById(id); }

  function setStatus(id, msg) {
    var el = $(id);
    if (el) el.textContent = msg || '';
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function telLink(num) {
    var a = el('a', null, num);
    a.href = 'tel:' + num;
    return a;
  }

  /* 결과마다 붙는 짧은 연결 줄(SPEC §7: 1366·132, 위험이 급하면 112) */
  function helpLine() {
    var p = el('p', 'help-line');
    p.appendChild(document.createTextNode('혼자 하기 어려우면 '));
    p.appendChild(telLink('1366'));
    p.appendChild(document.createTextNode('(24시간) · '));
    p.appendChild(telLink('132'));
    p.appendChild(document.createTextNode(' · 위험이 급하면 '));
    p.appendChild(telLink('112'));
    return p;
  }

  /* 화면 읽기 프로그램에 짧게 알린다 */
  function announce(msg) {
    var lr = $('live-region');
    if (!lr) return;
    lr.textContent = '';
    setTimeout(function () { lr.textContent = msg; }, 30);
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      if (res.status === 503) {
        var e = new Error('unavailable');
        e.unavailable = true;
        throw e;
      }
      if (!res.ok) {
        var err = new Error(url + ' http ' + res.status);
        err.status = res.status;
        throw err;
      }
      return res.json();
    });
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
        lbl.setAttribute('data-flag', item.id);
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.name = 'flags';
        cb.value = item.id;
        var span = document.createElement('span');
        span.textContent = item.label || item.id;
        cb.addEventListener('change', function (e) {
          var l = e.currentTarget.parentNode;
          if (l) l.classList.remove('auto-picked');
        });
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
      setStatus('diagnose-status', '피해 유형을 먼저 골라 주세요.');
      var firstRadio = form.querySelector('input[name="victim_type"]');
      if (firstRadio) firstRadio.focus();
      return;
    }
    if (intakePending) {
      var cf = $('auto-confirm');
      if (!cf || !cf.checked) {
        setStatus('diagnose-status', '자동으로 고른 항목을 먼저 확인해 주세요.');
        if (cf) cf.focus();
        return;
      }
    }
    var flags = [];
    var checked = form.querySelectorAll('input[name="flags"]:checked');
    for (var i = 0; i < checked.length; i++) flags.push(checked[i].value);

    setStatus('diagnose-status', '보호 제도를 정리하고 있습니다...');
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
        var rh = document.querySelector('#diagnose-results .results-head');
        if (rh) rh.focus();
      })
      .catch(function () {
        setStatus('diagnose-status', '결과를 불러오지 못했습니다. 잠시 뒤 다시 시도해 주세요.');
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
    if (item.id) card.id = 'card-' + item.id;
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

    var gate = null;
    if (item.notify_warning) {
      cardSeq += 1;
      var howtoId = 'howto-' + cardSeq;
      var warn = el('div', 'warn-box notify-box');
      warn.setAttribute('role', 'group');
      warn.setAttribute('aria-labelledby', 'notify-lead-' + cardSeq);
      var lead = el('strong', 'notify-lead', '신청하면 상대에게 알려질 수 있습니다');
      lead.id = 'notify-lead-' + cardSeq;
      warn.appendChild(lead);
      warn.appendChild(el('p', null, item.notify_warning));
      var ackLabel = el('label', 'notify-ack');
      var ack = document.createElement('input');
      ack.type = 'checkbox';
      ack.setAttribute('aria-controls', howtoId);
      ackLabel.appendChild(ack);
      ackLabel.appendChild(el('span', null, '상대에게 알려질 수 있다는 점을 확인했습니다'));
      warn.appendChild(ackLabel);
      card.appendChild(warn);
      gate = { id: howtoId, ack: ack };
    }

    if (item.timing) card.appendChild(row('시기', item.timing));

    function detailsBlock(lab, val) {
      var det = document.createElement('details');
      det.appendChild(el('summary', null, lab));
      det.appendChild(el('p', null, val));
      return det;
    }

    // 통지 경고가 있으면 쉬운 설명(신청 방법이 섞여 있다)과 신청처·서류는 경고를 확인한 뒤에만 펼친다.
    var howto = el('div', 'howto');
    if (item.plain) {
      var plain = document.createElement('div');
      plain.className = 'plain-box';
      if (!item.plain_reviewed) {
        plain.appendChild(badge('ai-badge', 'AI 생성(검수 전)'));
      }
      var pt = document.createElement('p');
      pt.textContent = item.plain;
      plain.appendChild(pt);
      if (gate) howto.appendChild(plain); else card.appendChild(plain);
    }
    if (item.where) howto.appendChild(detailsBlock('어디서 신청하나요', item.where));
    if (item.documents) howto.appendChild(detailsBlock('필요한 서류', item.documents));
    if (howto.childNodes.length) {
      if (gate) {
        howto.id = gate.id;
        howto.hidden = true;
        var locked = el('p', 'howto-locked', '위 경고를 확인하면 쉬운 설명과 신청 방법이 보입니다.');
        card.appendChild(locked);
        (function (ack, box, lockedEl) {
          ack.addEventListener('change', function () {
            box.hidden = !ack.checked;
            lockedEl.hidden = ack.checked;
            announce(ack.checked ? '쉬운 설명과 신청 방법이 펼쳐졌습니다.' : '신청 방법을 다시 접었습니다.');
          });
        })(gate.ack, howto, locked);
      }
      card.appendChild(howto);
    }
    if (item.limits) card.appendChild(detailsBlock('한계', item.limits));

    // 경로·노출·근거 조문은 '근거·자세히' 안으로(첫 화면 부담 줄이기).
    var hasBasis = Array.isArray(item.basis) && item.basis.length;
    if (item.path || item.exposure || hasBasis) {
      var more = document.createElement('details');
      more.className = 'more-detail';
      more.appendChild(el('summary', null, '근거·자세히'));
      if (item.path) more.appendChild(row('경로', item.path));
      if (item.exposure) more.appendChild(row('노출', item.exposure));
      if (hasBasis) {
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
        more.appendChild(chips);
      }
      card.appendChild(more);
    }

    card.appendChild(helpLine());
    if (llmOn && item.id) card.appendChild(buildAskBox(item.id));

    return card;
  }

  /* ---------- (LLM) 이게 무슨 뜻인가요? ---------- */
  function buildAskBox(itemId) {
    cardSeq += 1;
    var n = cardSeq;
    var wrap = el('div', 'llm-slot ask-box');
    var toggle = el('button', 'ask-toggle', '이게 무슨 뜻인가요?');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', 'ask-' + n);
    wrap.appendChild(toggle);

    var body = el('div', 'ask-body');
    body.id = 'ask-' + n;
    body.hidden = true;
    var lab = el('label', 'field-label', '이 제도에 대해 궁금한 점을 적어 주세요');
    lab.setAttribute('for', 'ask-q-' + n);
    body.appendChild(lab);
    var row = el('div', 'search-row');
    var input = document.createElement('input');
    input.type = 'text';
    input.id = 'ask-q-' + n;
    input.className = 'search-input';
    input.maxLength = 500;
    input.autocomplete = 'off';
    input.placeholder = '예: 신청하면 언제부터 효과가 있나요?';
    row.appendChild(input);
    var btn = el('button', null, '물어보기');
    btn.type = 'button';
    row.appendChild(btn);
    body.appendChild(row);
    var out = el('div', 'ask-out');
    out.setAttribute('aria-live', 'polite');
    body.appendChild(out);
    wrap.appendChild(body);

    toggle.addEventListener('click', function () {
      var open = body.hidden;
      body.hidden = !open;
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) input.focus();
    });

    function ask() {
      var q = input.value.trim();
      out.textContent = '';
      if (!q) { out.appendChild(el('p', 'status', '질문을 적어 주세요.')); input.focus(); return; }
      btn.disabled = true;
      out.appendChild(el('p', 'status', '답을 만들고 있습니다...'));
      postJSON('/api/answer', { question: q, item_ids: [itemId] })
        .then(function (d) {
          out.textContent = '';
          if (d && d.refused) {
            out.appendChild(el('p', 'notice', d.message || '이 질문에는 답할 수 없습니다.'));
            return;
          }
          var box = el('div', 'answer-box');
          var head = el('div', 'card-meta');
          if (d && d.ai_generated === false) {
            head.appendChild(badge('src-badge', '안내문 원문'));
            head.appendChild(el('span', 'meta', d.source === 'effect'
              ? '시점에 관한 질문은 AI가 만들지 않고 안내문의 신청 시점·처리 칸을 그대로 보여 드립니다.'
              : '알려지는지에 관한 질문은 AI가 만들지 않고 안내문의 통지 경고를 그대로 보여 드립니다.'));
          } else {
            head.appendChild(badge('ai-badge', 'AI 생성'));
            head.appendChild(el('span', 'meta', '안내문과 조문만 근거로 만든 답입니다. 틀릴 수 있으니 중요한 결정 전에는 꼭 확인해 주세요.'));
          }
          box.appendChild(head);
          box.appendChild(el('p', 'answer-text', (d && d.answer) || ''));
          var srcs = (d && Array.isArray(d.sources)) ? d.sources : [];
          var labels = [];
          for (var si = 0; si < srcs.length; si++) {
            if (srcs[si] && typeof srcs[si].label === 'string' && srcs[si].label) labels.push(srcs[si].label);
          }
          if (labels.length) box.appendChild(el('p', 'meta', '참고: ' + labels.join(' / ')));
          box.appendChild(helpLine());
          out.appendChild(box);
        })
        .catch(function (e) {
          out.textContent = '';
          if (e && e.unavailable) {
            out.appendChild(el('p', 'notice', LLM_DOWN_MSG));
            setLlm(false);
          } else {
            out.appendChild(el('p', 'notice', '답을 받지 못했습니다. 잠시 뒤 다시 시도해 주세요.'));
          }
          out.appendChild(helpLine());
        })
        .then(function () { btn.disabled = false; });
    }
    btn.addEventListener('click', ask);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); ask(); }
    });
    return wrap;
  }

  // 잔여 위험 카드: SPEC §7 — '이 기록에는 주소가 남을 수 있으니 등록·신청 전에 고려하세요' 수준의
  // 사전 안내만. 조회 주체·요건·방법을 담을 수 있는 경로·설명 문단은 그리지 않는다.
  function buildResidualCard(it) {
    var card = document.createElement('article');
    card.className = 'card residual';
    if (it.id) card.id = 'card-' + it.id;
    var head = document.createElement('div');
    head.className = 'card-head';
    if (it.urgency === 1) head.appendChild(badge('urgency-1', URGENCY_LABELS[1]));
    var t = document.createElement('h3');
    t.className = 'card-title';
    t.textContent = it.path || '주소가 남을 수 있는 기록';
    head.appendChild(t);
    card.appendChild(head);
    card.appendChild(el('p', null, '이 기록에는 주소가 남을 수 있으니 등록·신청 전에 고려하세요. 이 경로를 막는 제도는 없습니다.'));
    if (it.timing) card.appendChild(row('대비', it.timing));
    if (it.support) card.appendChild(el('p', 'card-note', it.support));
    if (llmOn && it.id) card.appendChild(buildAskBox(it.id));
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

    var resultsHead = el('h2', 'results-head', '내게 맞는 보호 제도');
    resultsHead.tabIndex = -1;
    root.insertBefore(resultsHead, root.firstChild);

    var printBtn = document.createElement('button');
    printBtn.type = 'button';
    printBtn.className = 'print-btn';
    printBtn.textContent = '결과 인쇄하기';
    printBtn.addEventListener('click', function () { window.print(); });
    root.appendChild(printBtn);

    var checklist = Array.isArray(data.checklist) ? data.checklist : [];
    var residual = Array.isArray(data.residual) ? data.residual : [];
    var TOP_MAX = 5;
    var top = [];
    var rest = [];
    for (var ci = 0; ci < checklist.length; ci++) {
      var c = checklist[ci];
      if (c.urgency === 1 && c.status !== 'pending' && top.length < TOP_MAX) top.push(c);
      else rest.push(c);
    }
    // '지금 먼저' 잔여 위험(예: 가해자가 먼저 낸 소송 — 다음 서면 송달 전에 대응)은 위쪽 묶음에 끼운다.
    var urgentRes = [];
    var otherRes = [];
    for (var ri = 0; ri < residual.length; ri++) {
      if (residual[ri] && residual[ri].urgency === 1) urgentRes.push(residual[ri]);
      else otherRes.push(residual[ri]);
    }

    if (top.length || urgentRes.length) {
      var h1 = el('h2', null, '지금 먼저 할 일');
      root.appendChild(h1);
      var ol = el('ol', 'now-list');
      var all = top.concat(urgentRes);
      for (var ti = 0; ti < all.length; ti++) {
        var li0 = document.createElement('li');
        var a0 = document.createElement('a');
        a0.href = '#card-' + all[ti].id;
        var isRes = urgentRes.indexOf(all[ti]) !== -1;
        a0.textContent = isRes ? (all[ti].path || all[ti].measure || '') + ' — 미리 대비' : (all[ti].measure || all[ti].id || '');
        li0.appendChild(a0);
        ol.appendChild(li0);
      }
      root.appendChild(ol);
      root.appendChild(helpLine());
      for (var t2 = 0; t2 < top.length; t2++) root.appendChild(buildCard(top[t2]));
      for (var t3 = 0; t3 < urgentRes.length; t3++) root.appendChild(buildResidualCard(urgentRes[t3]));
    }

    if (rest.length) {
      var moreBox = document.createElement('details');
      moreBox.className = 'more-block';
      if (!top.length) moreBox.open = true;
      moreBox.appendChild(el('summary', null, (top.length ? '다른 보호 제도 ' : '보호 제도 ') + rest.length + '건 더 보기'));
      for (var k2 = 0; k2 < rest.length; k2++) moreBox.appendChild(buildCard(rest[k2]));
      root.appendChild(moreBox);
    }
    if (!checklist.length) {
      var empty = document.createElement('p');
      empty.textContent = '고른 상황에 맞는 제도를 찾지 못했습니다. 항목을 바꿔 다시 확인해 보세요.';
      root.appendChild(empty);
    }

    if (otherRes.length) {
      var h2 = document.createElement('h2');
      h2.textContent = '제도로 막을 수 없는 경로 — 미리 대비해 주세요';
      root.appendChild(h2);
      var rintro = el('p', 'lead', '아래 기록에는 주소가 남을 수 있습니다. 등록·신청하기 전에 한 번 더 생각해 주세요.');
      root.appendChild(rintro);
      root.appendChild(helpLine());
      for (var r = 0; r < otherRes.length; r++) root.appendChild(buildResidualCard(otherRes[r] || {}));
    }

    var na = Array.isArray(data.not_applicable) ? data.not_applicable : [];
    if (na.length) {
      var h3 = document.createElement('h2');
      h3.textContent = '이 피해 유형에는 적용되지 않는 제도';
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
      p0.textContent = '찾은 단서가 없습니다. 그래도 내기 전에 한 번 더 읽어 봐 주세요.';
      root.appendChild(p0);
      return;
    }
    var h = document.createElement('h3');
    h.textContent = '찾은 단서 (' + spans.length + '건)';
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
        tb.textContent = s.tier === 2 ? '정밀' : '기본';
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
    lastSpans = tier1Spans.slice();
    lastText = text;
    setStatus('doc-status', '기본 점검을 마쳤습니다. 단서 ' + tier1Spans.length + '건을 찾았습니다.');
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
      setStatus('doc-status', '서류 내용을 먼저 붙여 넣어 주세요.');
      return;
    }
    setStatus('doc-status', '정밀 점검 중입니다...');
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
          setStatus('doc-status', '지금은 정밀 점검을 쓸 수 없습니다. 기본 점검 결과를 참고해 주세요.');
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
        lastSpans = merged.slice();
        lastText = text;
        setStatus('doc-status', '정밀 점검을 마쳤습니다. 단서 ' + merged.length + '건을 찾았습니다.');
      })
      .catch(function () {
        setStatus('doc-status', '지금은 정밀 점검을 쓸 수 없습니다. 기본 점검 결과를 참고해 주세요.');
      });
  }

  /* ---------- search ---------- */
  function runSearch() {
    var q = $('search-q').value.trim();
    var root = $('search-results');
    root.textContent = '';
    if (!q) {
      setStatus('search-status', '궁금한 점을 적어 주세요.');
      return;
    }
    setStatus('search-status', '찾고 있습니다...');
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
          notice.textContent = '지금은 간단한 낱말 검색으로 찾았습니다. 결과가 덜 정확할 수 있습니다.';
          root.appendChild(notice);
        }
        var list = [];
        if (Array.isArray(data)) list = data;
        else if (data && Array.isArray(data.results)) list = data.results;

        if (!list.length) {
          var p = document.createElement('p');
          p.textContent = '맞는 조문을 찾지 못했습니다. 다른 낱말로 다시 적어 보세요.';
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
        setStatus('search-status', '찾지 못했습니다. 잠시 뒤 다시 시도해 주세요.');
      });
  }

  /* ---------- (LLM) 안전한 문장으로 바꾸기 ---------- */
  function tokenize(t) {
    return t.match(/\s+|[^\s]+/g) || [];
  }

  // 낱말 단위 LCS 비교. 너무 길면 비교 없이 두 글을 그대로 보여 준다.
  function diffTokens(a, b) {
    var n = a.length, m = b.length;
    if (n * m > 400000) return null;
    var dp = [];
    for (var i = 0; i <= n; i++) {
      dp.push(new Uint16Array(m + 1));
    }
    for (var i2 = n - 1; i2 >= 0; i2--) {
      for (var j2 = m - 1; j2 >= 0; j2--) {
        dp[i2][j2] = a[i2] === b[j2] ? dp[i2 + 1][j2 + 1] + 1 : Math.max(dp[i2 + 1][j2], dp[i2][j2 + 1]);
      }
    }
    var ops = [];
    var x = 0, y = 0;
    while (x < n && y < m) {
      if (a[x] === b[y]) { ops.push(['=', a[x]]); x++; y++; }
      else if (dp[x + 1][y] >= dp[x][y + 1]) { ops.push(['-', a[x]]); x++; }
      else { ops.push(['+', b[y]]); y++; }
    }
    while (x < n) { ops.push(['-', a[x++]]); }
    while (y < m) { ops.push(['+', b[y++]]); }
    return ops;
  }

  function diffPane(title, ops, keep, tag, fallbackText) {
    var pane = el('div', 'diff-pane');
    pane.appendChild(el('h4', 'diff-title', title));
    var body = el('div', 'diff-body');
    if (!ops) {
      body.textContent = fallbackText;
    } else {
      for (var i = 0; i < ops.length; i++) {
        var op = ops[i][0], tok = ops[i][1];
        if (op === '=') body.appendChild(document.createTextNode(tok));
        else if (op === keep) {
          var m = document.createElement(tag);
          m.textContent = tok;
          body.appendChild(m);
        }
      }
    }
    pane.appendChild(body);
    return pane;
  }

  function runRewrite() {
    var text = $('doc-text').value;
    var out = $('doc-rewrite-result');
    if (!text || !text.trim()) {
      setStatus('doc-status', '서류 내용을 먼저 붙여 넣어 주세요.');
      return;
    }
    if (text.length > REWRITE_MAX) {
      setStatus('doc-status', REWRITE_MAX + '자 이하로 나누어 바꿔 주세요. (지금 ' + text.length + '자)');
      return;
    }
    if (text !== lastText) runTier1();
    var spans = [];
    for (var i = 0; i < lastSpans.length && spans.length < 60; i++) {
      var s = lastSpans[i];
      spans.push({ start: s.start, end: s.end, label: String(s.label || '').slice(0, 20) });
    }
    var btn = $('doc-rewrite');
    btn.disabled = true;
    setStatus('doc-status', '안전한 문장을 만들고 있습니다...');
    postJSON('/api/rewrite', { text: text, spans: spans })
      .then(function (d) {
        out.textContent = '';
        if (d && d.refused) {
          out.appendChild(el('p', 'notice', d.message || '이 문장은 바꿀 수 없습니다.'));
          out.hidden = false;
          setStatus('doc-status', '이 문장은 바꿀 수 없습니다.');
          return;
        }
        var after = (d && typeof d.rewritten === 'string') ? d.rewritten : '';
        var head = el('div', 'card-meta');
        head.appendChild(badge('ai-badge', 'AI 생성'));
        head.appendChild(el('span', 'meta', '제안일 뿐입니다. 뜻이 바뀌거나 없던 내용이 들어가지 않았는지 꼭 확인해 주세요.'));
        out.appendChild(head);

        var ops = diffTokens(tokenize(text), tokenize(after));
        var grid = el('div', 'diff-grid');
        grid.appendChild(diffPane('원래 문장', ops, '-', 'del', text));
        grid.appendChild(diffPane('바꾼 문장 제안', ops, '+', 'ins', after));
        out.appendChild(grid);

        var rem = (d && Array.isArray(d.remaining)) ? d.remaining : [];
        if (rem.length) {
          out.appendChild(el('p', 'notice', '아직 남아 있는 단서: ' + rem.join(', ') + ' — 직접 고쳐 주세요.'));
        }

        var lab = el('label', 'field-label', '바꾼 문장 (직접 선택해서 복사해 주세요)');
        lab.setAttribute('for', 'rewrite-copy');
        out.appendChild(lab);
        var ta = document.createElement('textarea');
        ta.id = 'rewrite-copy';
        ta.className = 'doc-text rewrite-copy';
        ta.readOnly = true;
        ta.rows = 6;
        ta.value = after;
        out.appendChild(ta);
        out.appendChild(helpLine());
        out.hidden = false;
        out.focus();
        setStatus('doc-status', '바꾼 문장 제안을 만들었습니다.');
      })
      .catch(function (e) {
        if (e && e.unavailable) {
          setStatus('doc-status', LLM_DOWN_MSG);
          setLlm(false);
        } else if (e && e.status === 504) {
          setStatus('doc-status', '시간이 너무 오래 걸려 멈췄습니다. 글을 더 짧게 나누어 다시 해 주세요.');
        } else if (e && e.status === 429) {
          setStatus('doc-status', '다른 요청을 처리하고 있습니다. 잠시 뒤 다시 해 주세요.');
        } else {
          setStatus('doc-status', '문장을 바꾸지 못했습니다. 잠시 뒤 다시 시도해 주세요.');
        }
      })
      .then(function () { btn.disabled = false; });
  }

  /* ---------- (LLM) 말로 설명하기 ---------- */
  function runIntake() {
    var ta = $('intake-text');
    var text = ta.value.trim();
    if (!text) {
      setStatus('intake-status', '상황을 먼저 적어 주세요.');
      ta.focus();
      return;
    }
    var btn = $('intake-btn');
    btn.disabled = true;
    setStatus('intake-status', '항목을 고르고 있습니다...');
    postJSON('/api/intake', { text: text })
      .then(function (d) {
        if (d && d.refused) {
          setStatus('intake-status', d.message || '이 내용으로는 항목을 고를 수 없습니다.');
          return;
        }
        applyIntake(d || {});
      })
      .catch(function (e) {
        if (e && e.unavailable) {
          setStatus('intake-status', LLM_DOWN_MSG);
          setLlm(false);
        } else {
          setStatus('intake-status', '항목을 고르지 못했습니다. 아래 항목을 직접 골라 주세요.');
        }
      })
      .then(function () { btn.disabled = false; });
  }

  function flagLabel(box) {
    var sp = box.parentNode ? box.parentNode.querySelector('span') : null;
    return sp ? sp.textContent : box.value;
  }

  function syncConfirmGate() {
    var submit = $('diagnose-submit');
    var cf = $('auto-confirm');
    if (!submit) return;
    submit.disabled = intakePending && !(cf && cf.checked);
  }

  // 체크만 미리 해 둔다. 사용자가 이미 고른 피해 유형·항목은 바꾸지 않고, 절대 자동 제출하지 않는다.
  // 고른 항목은 안내 상자에 하나씩 보여 주고 각각 뺄 수 있다. '확인했습니다'를 체크해야 제출된다.
  function applyIntake(d) {
    var form = $('diagnose-form');
    var prev = form.querySelectorAll('.auto-picked');
    for (var i = 0; i < prev.length; i++) prev[i].classList.remove('auto-picked');

    var pickedList = $('auto-picked-list');
    pickedList.textContent = '';
    var sugg = $('auto-victim');
    sugg.textContent = '';
    var picked = 0;

    if (typeof d.victim_type === 'string') {
      var vt = d.victim_type.replace(/[^a-z]/g, '');
      var radio = form.querySelector('input[name="victim_type"][value="' + vt + '"]');
      var current = form.querySelector('input[name="victim_type"]:checked');
      if (radio && !current) {
        radio.checked = true;
        radio.parentNode.classList.add('auto-picked');
        sugg.appendChild(document.createTextNode('피해 유형: ' + (VICTIM_LABELS[vt] || vt) + ' (자동으로 골랐습니다. 다르면 위에서 바꿔 주세요.)'));
        picked++;
      } else if (radio && current && current !== radio) {
        // 사용자가 이미 고른 유형은 덮어쓰지 않고 제안만 한다.
        sugg.appendChild(document.createTextNode('지금 고른 피해 유형은 ' + (VICTIM_LABELS[current.value] || current.value) +
          '입니다. 적은 내용으로는 ' + (VICTIM_LABELS[vt] || vt) + '에 가까워 보입니다. '));
        var sw = el('button', 'small-btn', VICTIM_LABELS[vt] + '(으)로 바꾸기');
        sw.type = 'button';
        sw.addEventListener('click', function () {
          radio.checked = true;
          sugg.textContent = '피해 유형을 ' + (VICTIM_LABELS[vt] || vt) + '(으)로 바꿨습니다.';
        });
        sugg.appendChild(sw);
      }
    }
    var flags = Array.isArray(d.flags) ? d.flags : [];
    var boxes = form.querySelectorAll('input[name="flags"]');
    for (var j = 0; j < boxes.length; j++) {
      var box = boxes[j];
      if (flags.indexOf(box.value) === -1) continue;
      if (!box.checked) {
        box.checked = true;
        box.parentNode.classList.add('auto-picked');
      }
      picked++;
      (function (b) {
        var li = el('li', 'auto-picked-item');
        li.appendChild(el('span', null, flagLabel(b)));
        var rm = el('button', 'small-btn', '빼기');
        rm.type = 'button';
        rm.setAttribute('aria-label', flagLabel(b) + ' 빼기');
        rm.addEventListener('click', function () {
          b.checked = false;
          b.parentNode.classList.remove('auto-picked');
          li.classList.add('removed');
          rm.disabled = true;
          rm.textContent = '뺐습니다';
          announce(flagLabel(b) + ' 항목을 뺐습니다.');
        });
        li.appendChild(rm);
        pickedList.appendChild(li);
      })(box);
    }

    var ul = $('auto-unclear');
    ul.textContent = '';
    var unclear = Array.isArray(d.unclear) ? d.unclear : [];
    for (var k = 0; k < unclear.length; k++) {
      ul.appendChild(el('li', null, '확인이 필요합니다: ' + unclear[k]));
    }
    var cf = $('auto-confirm');
    cf.checked = false;
    intakePending = true;
    syncConfirmGate();
    var notice = $('auto-notice');
    notice.hidden = false;
    setStatus('intake-status', picked ? ('항목 ' + picked + '개를 골랐습니다. 아래 목록을 확인해 주세요.') : '맞는 항목을 고르지 못했습니다. 아래에서 직접 골라 주세요.');
    notice.focus();
  }

  /* ---------- features ---------- */
  function setLlm(on) {
    llmOn = !!on;
    var slots = document.querySelectorAll('.llm-slot');
    for (var i = 0; i < slots.length; i++) slots[i].hidden = !llmOn;
  }

  function loadFeatures() {
    fetch('/api/features', { headers: { 'Accept': 'application/json' } })
      .then(function (res) { return res.ok ? res.json() : { llm: false }; })
      .then(function (d) { setLlm(d && d.llm === true); })
      .catch(function () { setLlm(false); });
  }

  /* ---------- safety notice ---------- */
  function initSafety() {
    var btn = $('safety-toggle');
    var body = $('safety-body');
    btn.addEventListener('click', function () {
      var open = body.hidden;
      body.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      btn.textContent = open ? '안내 접기' : '안전 안내 다시 보기';
      if (!open) {
        var t = $('tab-diagnose');
        if (t) t.focus();
      }
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
    $('doc-rewrite').addEventListener('click', runRewrite);
    $('intake-btn').addEventListener('click', runIntake);
    $('auto-confirm').addEventListener('change', syncConfirmGate);
    // 인쇄할 때는 접어 둔 '더 보기'·'근거·자세히'를 모두 펼친다.
    window.addEventListener('beforeprint', function () {
      var ds = document.querySelectorAll('#diagnose-results details');
      for (var i = 0; i < ds.length; i++) ds[i].open = true;
    });
    initSafety();
    loadFeatures();

    // Esc 를 1초 안에 두 번 누르면 빠른 나가기.
    var lastEsc = 0;
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var now = Date.now();
      if (now - lastEsc < 1000) quickExit();
      lastEsc = now;
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
