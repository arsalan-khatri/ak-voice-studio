const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const source = fs.readFileSync('static/index.html', 'utf8');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {value: '', hidden: true, disabled: false, style: {}, focus() {this.focused=true;}});
  return elements.get(id);
};
const calls=[];
const state={keys:[{id:'a',label:'Primary',masked:'••••1234',status:'Working'}],active:'a',events:[{message:'Auto switch: Backup → Primary'}]};
const context = vm.createContext({
  $:element, esc:String, setInterval(){}, renderSegments(){},
  store:{get(){return '';},set(){}},
  status(el,text){el.textContent=text;},
  loadVoices:async()=>{},
  api:async(url,options)=>{ calls.push({url,options}); return url==='/api/models' ? {models:[{id:'gemini-3.8-flash-tts',name:'Flash TTS'}]} : state; }
});
const block=source.split('/* ================= API keys and live model catalog ================= */')[1].split('/* ================= navigation ================= */')[0];
vm.runInContext(block,context);
(async()=>{
  element('#addKeyToggle').onclick();
  assert.equal(element('#addKeyForm').hidden,false);
  element('#keyInput').value='fake-test-key'; element('#keyLabel').value='Primary';
  await element('#saveKeyBtn').onclick();
  assert.equal(JSON.parse(calls[0].options.body).key,'fake-test-key');
  assert.equal(element('#keyInput').value,'');
  assert.equal(element('#keyWarn').hidden,true);
  assert(element('#keyList').innerHTML.includes('Active'));
  assert(element('#keyEvents').textContent.includes('Auto switch'));
  const btn={disabled:false,dataset:{keySelect:'b'}};
  await element('#keyList').onclick({target:{closest:()=>btn}});
  assert(calls.some(c=>c.url==='/api/keys/b/select' && c.options.method==='POST'));
  assert(element('#model').innerHTML.includes('gemini-3.8-flash-tts'));
  console.log('Frontend keys: add, clear secret, active display, auto-switch notice, manual selection and models passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
