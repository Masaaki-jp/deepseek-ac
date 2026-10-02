import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';

function walk(p) {
  return fs.readdirSync(p, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory() ? walk(path.join(p, e.name)) : [path.join(p, e.name)]
  );
}

const files = walk('dist');
assert(
  !files.some((f) => /keystatic|\/preview\/|\.mdoc$|\.env/.test(f)),
  '下書き・管理画面が出力されています'
);

const origin = 'https://deepseek.ac';
for (const f of files.filter((f) => f.endsWith('.html'))) {
  const text = fs.readFileSync(f, 'utf8');
  for (const [, url] of text.matchAll(/(?:href|src)="([^"#]+)"/g)) {
    const resolved = new URL(url, origin);
    if (resolved.origin !== origin) continue;
    const target = path.join('dist', decodeURIComponent(resolved.pathname));
    assert(
      fs.existsSync(target) || fs.existsSync(path.join(target, 'index.html')),
      `${f}: broken ${url}`
    );
  }
}
console.log(`PASS: ${files.length} outputs; internal links`);
