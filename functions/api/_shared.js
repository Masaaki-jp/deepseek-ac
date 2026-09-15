export const cookieName='__Host-deepseek-cms-state';
export const cookie=(value,age=600)=>`${cookieName}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${age}`;
export function config(env,request){
 const origin=new URL(env.CMS_ORIGIN).origin;
 if(!origin.startsWith('https://')||new URL(request.url).origin!==origin||!env.GITHUB_CLIENT_ID||!env.GITHUB_CLIENT_SECRET)throw Error('configuration');
 return {origin,repo:'Masaaki-jp/deepseek-ac',owner:'Masaaki-jp'};
}
export const headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff'};
export function failure(status=400){return new Response('CMS認証を完了できませんでした。管理画面から再度ログインしてください。',{status,headers:{...headers,'Content-Type':'text/plain; charset=utf-8','Set-Cookie':cookie('',0)}});}
