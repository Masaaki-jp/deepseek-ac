import test from 'node:test';import assert from 'node:assert/strict';
import {onRequestGet as start} from '../functions/api/auth.js';
import {onRequestGet as callback} from '../functions/api/callback.js';
const env={CMS_ORIGIN:'https://deepseek.ac',GITHUB_CLIENT_ID:'test',GITHUB_CLIENT_SECRET:'test-secret'};
test('auth refuses unconfigured and foreign origins',async()=>{assert.equal((await start({request:new Request('https://deepseek.ac/api/auth'),env:{}})).status,503);assert.equal((await start({request:new Request('https://evil.example/api/auth'),env})).status,503);});
test('auth binds random state to secure host cookie and exact callback',async()=>{const r=await start({request:new Request('https://deepseek.ac/api/auth'),env});assert.equal(r.status,302);const u=new URL(r.headers.get('location'));assert.equal(u.searchParams.get('redirect_uri'),'https://deepseek.ac/api/callback');assert.match(r.headers.get('set-cookie'),/HttpOnly; Secure; SameSite=Lax/);assert.equal(u.searchParams.get('scope'),null);});
test('callback rejects missing or mismatched state before network',async()=>{const old=globalThis.fetch;globalThis.fetch=()=>{throw Error('network must not run')};try{const r=await callback({request:new Request('https://deepseek.ac/api/callback?code=fake&state=bad'),env});assert.equal(r.status,400);assert(!await r.text().then(x=>x.includes('test-secret')));}finally{globalThis.fetch=old;}});
test('callback delivers token only to configured opener and clears cookie',async()=>{
 const state='a'.repeat(72),old=globalThis.fetch;let calls=0;
 globalThis.fetch=async()=>Response.json([{access_token:'mock-token'},{login:'Masaaki-jp'},{permissions:{push:true}}][calls++]);
 try{const r=await callback({request:new Request(`https://deepseek.ac/api/callback?code=test&state=${state}`,{headers:{cookie:`__Host-deepseek-cms-state=${state}`}}),env});const body=await r.text();assert.equal(r.status,200);assert.equal(calls,3);assert.match(body,/e.source!==window.opener/);assert.match(body,/e.origin!==origin/);assert.match(body,/authorization:github:success/);assert.match(r.headers.get('set-cookie'),/Max-Age=0/);assert.equal(r.headers.get('cache-control'),'no-store');assert(!body.includes('test-secret'));}finally{globalThis.fetch=old;}
});
test('callback refuses a different GitHub user',async()=>{
 const state='b'.repeat(72),old=globalThis.fetch;let calls=0;
 globalThis.fetch=async()=>Response.json([{access_token:'mock-token'},{login:'different-user'}][calls++]);
 try{const r=await callback({request:new Request(`https://deepseek.ac/api/callback?code=test&state=${state}`,{headers:{cookie:`__Host-deepseek-cms-state=${state}`}}),env});assert.equal(r.status,403);assert.equal(calls,2);assert(!(await r.text()).includes('mock-token'));}finally{globalThis.fetch=old;}
});
