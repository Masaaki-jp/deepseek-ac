import test from 'node:test';import assert from 'node:assert/strict';import {lessons,markdown,html} from '../src/lib/content.mjs';
const sample={id:'ds-999',title:'表示検証用',summary:'架空の検証データ',status:'draft',minutes:1,body:'## 学習目標\n\n安全に確認する。'};
test('empty content directory is supported',()=>{assert.deepEqual(lessons(),[]);assert.deepEqual(lessons(true),[]);});
test('shared content retains verification status',()=>{assert(markdown(sample).includes('未検証'));assert(html(sample).includes('未検証'));assert(html(sample).includes(sample.title));});
test('raw script never rendered',()=>{assert(!html({...sample,body:'<script>alert(1)</script>'}).includes('<script>'));});
test('relative references resolve consistently',()=>{const l={...sample,body:'![図](/images/example.svg)\n[関連](../ds-002/)'};assert(markdown(l).includes('https://deepseek.ac/images/example.svg'));assert(html(l).includes('https://deepseek.ac/ja/lessons/ds-002/'));});
