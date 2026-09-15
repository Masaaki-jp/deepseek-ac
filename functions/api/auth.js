import {config,cookie,headers,failure} from './_shared.js';
export async function onRequestGet({request,env}){
 try{
 const {origin}=config(env,request);
 const state=crypto.randomUUID()+crypto.randomUUID();
 const url=new URL('https://github.com/login/oauth/authorize');
 url.search=new URLSearchParams({client_id:env.GITHUB_CLIENT_ID,redirect_uri:origin+'/api/callback',state,allow_signup:'false'}).toString();
 return new Response(null,{status:302,headers:{...headers,Location:url.href,'Set-Cookie':cookie(state)}});
 }catch{return failure(503);}
}
