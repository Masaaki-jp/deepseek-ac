import fs from 'node:fs';import {spawnSync} from 'node:child_process';import assert from 'node:assert/strict';
const file='content/lessons/ds-999.mdoc';assert(!fs.existsSync(file));
const build=()=>{const r=spawnSync(process.execPath,['node_modules/astro/astro.js','build'],{encoding:'utf8',env:{...process.env,ASTRO_TELEMETRY_DISABLED:'1'}});assert.equal(r.status,0,r.stderr);};
try{
fs.mkdirSync('content/lessons',{recursive:true});
fs.writeFileSync(file,`---\nid: ds-999\ntitle: 公開処理の一時テスト\nsummary: 検証専用\nstatus: published\nminutes: 1\npublished: '2026-09-14'\nupdated: '2026-09-14'\n---\n## 注意事項\n\n架空データのみ。API操作は未検証です。\n`);
build();const page=fs.readFileSync('dist/ja/lessons/ds-999/index.html','utf8');const md=fs.readFileSync('dist/ja/lessons/ds-999.md','utf8');assert(page.includes('架空データのみ。API操作は未検証です。'));assert(md.includes('架空データのみ。API操作は未検証です。'));assert(page.includes('検証日: 未検証'));assert(md.includes('検証日: 未検証'));assert(fs.readFileSync('dist/sitemap.xml','utf8').includes('/ds-999/'));assert(fs.readFileSync('dist/llms.txt','utf8').includes('ds-999.md'));console.log('PASS: published HTML/Markdown and discovery generated');
}finally{fs.unlinkSync(file);build();}
assert(!fs.existsSync('dist/ja/lessons/ds-999.md'));console.log('PASS: temporary entry removed and production rebuilt');
