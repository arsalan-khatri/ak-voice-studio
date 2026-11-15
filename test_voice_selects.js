const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync('static/index.html','utf8');
const elements=new Map();
function $(id){
  if(!elements.has(id)) elements.set(id,{options:[],_value:'',style:{},textContent:'',
    get value(){return this._value;},set value(v){this._value=v;},
    set innerHTML(text){this.options=[...text.matchAll(/<option value="([^"]*)">/g)].map(m=>({value:m[1]}));this._value=this.options[0]?.value||'';}
  });
  return elements.get(id);
}
$('#speechLanguage').value='ur-PK';
let result={voices:[]};
const context=vm.createContext({$,esc:String,encodeURIComponent,renderSegments(){},store:{get(k,d){return d;},set(){}},api:async()=>result});
vm.runInContext(html.slice(html.indexOf('const PREBUILT ='),html.indexOf('async function loadVoices(')),context);
vm.runInContext(html.slice(html.indexOf('let languageRevision ='),html.indexOf('$("#voiceList").onclick')),context);
(async()=>{
  await vm.runInContext('refreshLanguageVoices()',context);
  assert.equal($('#spk1v').value,''); assert.equal($('#spk2v').value,'');
  assert.equal($('#spk1v').options.length,31);
  $('#spk1v').value='Puck'; $('#spk2v').value='Kore';
  await vm.runInContext('refreshLanguageVoices()',context);
  assert.equal($('#spk1v').value,'Puck'); assert.equal($('#spk2v').value,'Kore');
  result={voices:[{id:'urdu-male',gender:'male',language_code:'ur-PK'}, {id:'urdu-female',gender:'female',language_code:'ur-PK'}]};
  await vm.runInContext('refreshLanguageVoices()',context);
  assert.equal($('#spk1v').value,'urdu-male'); assert.equal($('#spk2v').value,'urdu-female');
  assert.equal($('#spk1v').options.length,2);
  $('#speechLanguage').value='';
  await vm.runInContext('refreshLanguageVoices()',context);
  assert.equal($('#spk1v').options.length,30);
  console.log('Automatic regional matching, empty-catalog manual selection, and preserved manual choices passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
