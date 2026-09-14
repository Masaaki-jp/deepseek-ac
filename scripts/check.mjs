import fs from 'node:fs';import path from 'node:path';import assert from 'node:assert/strict';import {lessons,markdown} from '../src/lib/content.mjs';
function walk(p){return fs.readdirSync(p,{withFileTypes:true}).flatMap(e=>e.isDirectory()?walk(path.join(p,e.name)):[path.join(p,e.name)]);}
const files=walk('dist');
assert(!files.some(f=>/keystatic|\/preview\/|\.mdoc$|\.env/.test(f)),'下書き・管理画面が出力されています');
for(const f of files.filter(f=>f.endsWith('.html'))){const text=fs.readFileSync(f,'utf8');for(const [,url] of text.matchAll(/(?:href|src)="([^"#]+)"/g)){if(!url.startsWith('/')||url.startsWith('//'))continue;const target=path.join('dist',url.split(/[?#]/)[0]);assert(fs.existsSync(target)||fs.existsSync(path.join(target,'index.html')),`${f}: broken ${url}`);}}
for(const l of lessons())assert.equal(fs.readFileSync(`dist/ja/lessons/${l.id}.md`,'utf8'),markdown(l));
for(const l of lessons(true).filter(l=>l.status==='draft')){assert(!files.some(f=>f.includes(l.id)),'下書きファイルが公開されています');for(const f of ['dist/llms.txt','dist/sitemap.xml','dist/ja/lessons/index.html'])assert(!fs.readFileSync(f,'utf8').includes(l.title),'下書きが一覧に含まれています');}
console.log(`PASS: ${files.length} outputs; internal links, draft exclusion, Markdown parity`);
