// 1단계 서류 점검 CLI. stdin: {"text": "..."} → stdout: {"spans": [...]}
// 입력 본문은 절대 stderr로 내보내지 않는다.
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const detectPath = resolve(here, '../web/static/detect.js');
const { detectSpans } = require(detectPath);

let chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const raw = Buffer.concat(chunks).toString('utf8');

let text = '';
try {
  const parsed = JSON.parse(raw || '{}');
  if (parsed && typeof parsed.text === 'string') text = parsed.text;
} catch (e) {
  text = '';
}

const spans = detectSpans(text);
process.stdout.write(JSON.stringify({ spans }));
