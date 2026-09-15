import {config,cookie,cookieName,headers,failure} from './_shared.js';
export async function onRequestGet({request,env}){
 try{
 const {origin,repo,owner}=config(env,request);
 const url=new URL(request.url),state=url.searchParams.get('state'),code=url.searchParams.get('code');
 const saved=(request.headers.get('cookie')||'').split(';').map(x=>x.trim()).find(x=>x.startsWith(cookieName+'='))?.slice(cookieName.length+1);
 if(!state||state.length!==72||state!==saved||!code||url.searchParams.has('error'))return failure();
 const response=await fetch('https://github.com/login/oauth/access_token',{method:'POST',headers:{Accept:'application/json','Content-Type':'application/json'},body:JSON.stringify({client_id:env.GITHUB_CLIENT_ID,client_secret:env.GITHUB_CLIENT_SECRET,code,redirect_uri:origin+'/api/callback'}),signal:AbortSignal.timeout(15000)});
 if(!response.ok)return failure(502);
 const data=await response.json();if(!data.access_token)return failure(401);
 const ghHeaders={Authorization:`Bearer ${data.access_token}`,Accept:'application/vnd.github+json','User-Agent':'deepseek-ac-cms'};
 const user=await fetch('https://api.github.com/user',{headers:ghHeaders,signal:AbortSignal.timeout(15000)});
 if(!user.ok||(await user.json()).login?.toLowerCase()!==owner.toLowerCase())return failure(403);
 const access=await fetch(`https://api.github.com/repos/${repo}`,{headers:ghHeaders,signal:AbortSignal.timeout(15000)});
 if(!access.ok||!(await access.json()).permissions?.push)return failure(403);
 const message=JSON.stringify('authorization:github:success:'+JSON.stringify({token:data.access_token,provider:'github'})).replace(/</g,'\\u003c');
 const nonce=crypto.randomUUID();
 const script=`const origin=${JSON.stringify(origin)};if(window.opener){window.addEventListener('message',function receive(e){if(e.origin!==origin||e.source!==window.opener||e.data!=='authorizing:github')return;window.removeEventListener('message',receive);window.opener.postMessage(${message},origin);window.close();});window.opener.postMessage('authorizing:github',origin);}`;
 return new Response(`<!doctype html><html lang="ja"><meta charset="utf-8"><title>CMS認証</title><p>認証しました。管理画面に戻ります。</p><script nonce="${nonce}">${script}</script></html>`,{headers:{...headers,'Content-Type':'text/html; charset=utf-8','Set-Cookie':cookie('',0),'Content-Security-Policy':`default-src 'none'; script-src 'nonce-${nonce}'; frame-ancestors 'none'; base-uri 'none'`}});
 }catch{return failure(502);}
}
