export function setup(CMS,h){
CMS.init({config:{load_config_file:false,backend:{name:'github',repo:'Masaaki-jp/deepseek-ac',branch:'main',base_url:location.origin,auth_endpoint:'api/auth'},publish_mode:'editorial_workflow',media_folder:'public/images/lessons',public_folder:'/images/lessons',site_url:location.origin,display_url:location.origin,collections:[{name:'lessons',label:'教材',folder:'content/lessons',extension:'mdoc',format:'frontmatter',create:true,identifier_field:'id',slug:'{{id}}',summary:'{{id}} — {{title}}',fields:[
{name:'id',label:'教材ID（作成後は変更しない）',widget:'string',pattern:['^ds-[0-9]{3}$','ds-004 の形式で入力']},
{name:'title',label:'タイトル',widget:'string'},
{name:'summary',label:'概要',widget:'text'},
{name:'status',label:'サイト公開対象',widget:'select',options:[{label:'下書き（公開対象外）',value:'draft'},{label:'公開対象（公開操作後にサイトへ反映）',value:'published'}],default:'draft'},
{name:'minutes',label:'所要時間（分）',widget:'number',value_type:'int',min:1,default:15},
...['published','updated','verified'].map((name,i)=>({name,label:['公開日（初回公開時に入力）','更新日','検証日（実際に検証した場合のみ）'][i],widget:'datetime',format:'YYYY-MM-DD',date_format:'YYYY-MM-DD',time_format:false,required:false,default:''})),
{name:'environment',label:'使用環境・未検証の範囲',widget:'text',required:false},
{name:'body',label:'本文（Markdown）',widget:'markdown'}]}]}});
CMS.registerPreviewStyle('body{font-family:system-ui,sans-serif;color:#152b48;padding:24px;line-height:1.9;max-width:780px;margin:auto}pre{overflow:auto;background:#f1f4f8;padding:16px}img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:8px}',{raw:true});
CMS.registerPreviewTemplate('lessons',({entry,widgetFor})=>h('article',{},h('p',{},'編集プレビュー：保存のみでは本番に反映されません。'),h('h1',{},entry.getIn(['data','title'])),h('p',{},entry.getIn(['data','summary'])),h('p',{},'検証日: '+(entry.getIn(['data','verified'])||'未検証')),widgetFor('body')));

}
