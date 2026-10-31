"""앱 경로(/api/answer) 측정 — 배포된 운영 앱을 ssh 터널로 부른다.

- 근거 질문 10개(tools/eval_llm.py QUESTIONS) x 3회: 앱 입력·출력 가드 + GPU /llm/answer 전체 흐름.
- 가드 세트(redteam 40, guard_holdout 20)를 J1 카드 질문으로 보내 거절·오거절을 센다.
사용: ssh -L 18080:127.0.0.1:8080 hk-ops 를 연 뒤
  python3 -I tools/eval_app_llm.py [out.json]   (기본 data/eval/results_app_llm.json)
합성 데이터만 쓴다. 응답 본문은 저장하지 않고 판정값만 남긴다.
"""
import json, sys, time, importlib.util, urllib.request, urllib.error
import pathlib
ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
s = importlib.util.spec_from_file_location('e', ROOT + '/tools/eval_llm.py'); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
B = 'http://127.0.0.1:18080'
OUT = sys.argv[1] if len(sys.argv) > 1 else ROOT + '/data/eval/results_app_llm.json'
def post(p, body):
    r = urllib.request.Request(B + p, data=json.dumps(body).encode(), headers={'content-type': 'application/json'})
    try:
        return json.load(urllib.request.urlopen(r, timeout=180))
    except urllib.error.HTTPError as e:
        return {'http': e.code}
res = {'answer_runs': [], 'guard': {}}
for q in m.QUESTIONS:
    for k in range(3):
        t = time.time(); d = post('/api/answer', {'question': q['q'], 'item_ids': q['context']})
        res['answer_runs'].append({'id': q['id'], 'answerable': q['answerable'], 'refused': bool(d.get('refused')), 'http503': d.get('http') == 503,
                                   'grounded': bool(d.get('grounded')), 'citations': d.get('citations', []),
                                   'cites_expected': bool(set(d.get('citations') or []) & set(q['expect'])),
                                   'latency_s': round(time.time() - t, 2)})
for name, path in (('redteam', 'redteam.json'), ('holdout', 'guard_holdout.json')):
    data = json.load(open(f'{ROOT}/data/eval/{path}')); items = data['items'] if isinstance(data, dict) else data
    tp = fn = fp = tn = 0
    for it in items:
        d = post('/api/answer', {'question': it['q'], 'item_ids': ['J1']})
        ref = bool(d.get('refused'))
        res.setdefault('guard_http', []).append(d.get('http')) if d.get('http') else None
        if it['expect_refuse']: tp += ref; fn += not ref
        else: fp += ref; tn += not ref
    res['guard'][name] = {'refusal': f'{tp}/{tp+fn}', 'false_refusal': f'{fp}/{fp+tn}'}
A = [r for r in res['answer_runs'] if r['answerable']]; N = [r for r in res['answer_runs'] if not r['answerable']]
res['summary'] = {
  'answerable_answered': f"{sum(r['grounded'] and not r['refused'] for r in A)}/{len(A)}",
  'answerable_refused_by_guard': f"{sum(r['refused'] for r in A)}/{len(A)}",
  'answerable_cites_expected': f"{sum(r['cites_expected'] for r in A)}/{len(A)}",
  'unanswerable_not_found': f"{sum((not r['grounded']) and not r['refused'] for r in N)}/{len(N)}",
  'unanswerable_refused_by_guard': f"{sum(r['refused'] for r in N)}/{len(N)}",
  'per_question_refused': {q['id']: sum(r['refused'] for r in res['answer_runs'] if r['id'] == q['id']) for q in m.QUESTIONS},
  'http503': sum(r['http503'] for r in res['answer_runs']),
  'latency_mean_s': round(sum(r['latency_s'] for r in res['answer_runs']) / len(res['answer_runs']), 2),
}
res['generated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
json.dump(res, open(OUT, 'w'), ensure_ascii=False, indent=1)
print(json.dumps(res['summary'], ensure_ascii=False, indent=1)); print(json.dumps(res['guard'], ensure_ascii=False))
