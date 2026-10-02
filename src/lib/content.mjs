import fs from 'node:fs';
import path from 'node:path';
import matter from 'gray-matter';
import {marked} from 'marked';
import sanitize from 'sanitize-html';
export const origin='https://deepseek.ac';
export const fixed=['/ja/','/ja/start/','/ja/lessons/','/ja/books/','/ja/about/','/ja/case-study/','/ja/policy/'];
export function lessons(includeDrafts=false){return (fs.existsSync('content/lessons')?fs.readdirSync('content/lessons'):[]).filter(f=>f.endsWith('.mdoc')).map(f=>{
 const {data,content}=matter(fs.readFileSync(path.join('content/lessons',f),'utf8'));
 const id=f.replace(/\.mdoc$/,''); if(!/^ds-\d{3}$/.test(id)) throw Error(`Invalid ID: ${id}`);
 if(!data.title||!['draft','published'].includes(data.status))throw Error(`Invalid metadata: ${id}`);
 if(data.status==='published'&&(!data.published||!data.updated))throw Error(`公開日・更新日が必要: ${id}`);
 if(content.includes('{%'))throw Error('公開Markdownと一致しないMarkdocタグは使用できません');
 return {...data,id,body:content.trim()};
 }).filter(l=>includeDrafts||l.status==='published').sort((a,b)=>a.id.localeCompare(b.id));}
function date(v){return v instanceof Date?v.toISOString().slice(0,10):v||'未記載';}
function absoluteLinks(body,id){
 const base=`${origin}/ja/lessons/${id}/`;
 return body.replace(/(!?\[[^\]]*\]\()([^\s)]+)([^)]*\))/g,(_,a,url,z)=>a+new URL(url,base).href+z).replace(/^(\[[^\]]+\]:\s*)(\S+)/gm,(_,a,url)=>a+new URL(url,base).href);
}
export function markdown(l){return `# ${l.title}\n\n${l.summary}\n\n- 教材ID: ${l.id}\n- 公開状態: ${l.status==='published'?'公開対象':'下書き'}\n- 所要時間: 約${l.minutes}分\n- 公開日: ${l.published?date(l.published):'未公開'}\n- 更新日: ${date(l.updated)}\n- 検証日: ${l.verified?date(l.verified):'未検証'}\n- 使用環境: ${l.environment||'未確認'}\n\n${absoluteLinks(l.body,l.id)}\n\n---\n再利用条件: https://deepseek.ac/ja/policy/\n`;}
export function html(l){return sanitize(marked.parse(markdown(l)),{allowedTags:sanitize.defaults.allowedTags.concat(['img']),allowedAttributes:{...sanitize.defaults.allowedAttributes,img:['src','alt','width','height'],code:['class']}});}
