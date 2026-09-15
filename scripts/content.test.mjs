import test from 'node:test';import assert from 'node:assert/strict';import {lessons,markdown,html} from '../src/lib/content.mjs';
test('drafts excluded by default',()=>{assert(lessons().every(l=>l.status==='published'));assert(lessons(true).length>=3)});
test('shared content retains caveats and verification status',()=>{for(const l of lessons(true)){assert(markdown(l).includes('未検証'));assert(html(l).includes('未検証'));assert(markdown(l).includes('## 学習目標')); assert(html(l).includes(l.title));}});
test('raw script never rendered',()=>{assert(!html({...lessons(true)[0],body:'<script>alert(1)</script>'}).includes('<script>'));});

test('relative references resolve consistently',()=>{const l={...lessons(true)[0],body:'![図](/images/example.svg)\n[関連](../ds-002/)'};assert(markdown(l).includes('https://deepseek.ac/images/example.svg'));assert(html(l).includes('https://deepseek.ac/ja/lessons/ds-002/'));});

test('API snippet matches the tested file',async()=>{const {readFileSync}=await import('node:fs');const body=lessons(true).find(l=>l.id==='ds-003').body;assert(body.includes(readFileSync('examples/ds-003/first_call.py','utf8')));});
