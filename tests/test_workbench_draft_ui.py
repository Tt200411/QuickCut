"""Exercise actual inline edit handlers without loading models or a browser app."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_draft_handlers_preserve_inputs_on_failure_and_send_trims(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the inline browser handlers")
    html = (Path(__file__).parents[1] / "quickcut/web/index.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    handlers = script[script.index("function reviewKey"):script.index("function drawReview")]
    handlers += script[script.index("async function changeSelection"):script.index("function drawResult")]
    harness = r'''
const assert=require('node:assert/strict');
let current='job',draftView,editPending=false,reviewDirty=false,editVersion=0,saveTimer,lastView='';
const elements=new Map(),inputs=new Map(),storage=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,{disabled:false,dataset:{},textContent:'',checked:false});return elements.get(id);};
const localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
const setTimeout=()=>1,clearTimeout=()=>{};
const document={querySelector:key=>inputs.get(key),querySelectorAll:()=>[]};
let request,redraws=0,messages=[];
function toast(value){messages.push(value);}
function drawReview(draft){redraws++;draftView=draft;}
async function poll(){}
function view(revision=0){return {draft_id:'frozen',review_revision:revision,beats:[{beat_id:'b',purpose:'开场',shots:[{candidate_id:'c1',segment_id:'s1',shot_index:0,available_range_us:{begin:1000000,end:16000000}}],available_candidates:[{candidate_id:'c1'},{candidate_id:'c2'}]}]};}
function fields(){
  inputs.set('[data-narration="b"]',{value:'编辑后的旁白。'});
  inputs.set('[data-beat="b"][data-shot="0"][data-edge="begin"]',{value:'3.123456'});
  inputs.set('[data-beat="b"][data-shot="0"][data-edge="end"]',{value:'8.654321'});
  inputs.set('select[aria-label="替换开场镜头1"]',{value:'c2'});
}
'''
    checks = r'''
(async()=>{
  draftView=view();fields();
  let sent;
  request=async(url,options)=>{sent=JSON.parse(options.body);throw new Error('invalid selection');};
  await changeSelection('b',0,'c2');
  assert.equal(redraws,0,'A failed replace must not redraw away pending input');
  assert.equal(inputs.get('[data-narration="b"]').value,'编辑后的旁白。');
  assert.equal(inputs.get('[data-beat="b"][data-shot="0"][data-edge="begin"]').value,'3.123456');
  assert.equal(inputs.get('select[aria-label="替换开场镜头1"]').value,'c1');
  assert.deepEqual(sent.draft_boundaries[0],{beat_id:'b',candidate_id:'c1',segment_id:'s1',source_range_us:{begin:3123456,end:8654321}});
  assert.deepEqual(sent.selection_overrides,{b:['c2']});
  assert.equal(sent.confirmed,undefined,'Saving a draft must not approve it');
  assert.equal(editPending,false);

  // Reload before an autosave response recovers the exact typed values.
  inputs.get('[data-narration="b"]').value='服务旧内容';
  inputs.get('[data-beat="b"][data-shot="0"][data-edge="begin"]').value='1';
  restoreReview();
  assert.equal(inputs.get('[data-narration="b"]').value,'编辑后的旁白。');
  assert.equal(inputs.get('[data-beat="b"][data-shot="0"][data-edge="begin"]').value,'3.123456');

  // Inputs typed while saving must stay dirty and be saved with the new revision.
  let resolve;
  request=async()=>new Promise(done=>resolve=done);
  const pending=saveReview();
  inputs.get('[data-narration="b"]').value='保存期间又修改。';
  reviewChanged();resolve({draft:view(1)});await pending;
  assert.equal(draftView.review_revision,1);
  assert.equal(reviewDirty,true);
  assert.equal(inputs.get('[data-narration="b"]').value,'保存期间又修改。');
  assert.equal(JSON.parse(localStorage.getItem(localDraftKey(current))).review_revision,1);
  assert.equal(redraws,0);

  request=async(url,options)=>{sent=JSON.parse(options.body);return {draft:view(2)};};
  await saveReview();
  assert.equal(sent.review_revision,1);
  assert.equal(sent.draft_text_edits.b,'保存期间又修改。');
  assert.equal(reviewDirty,false);
  assert.equal(localStorage.getItem(localDraftKey(current)),null);
  assert.equal($('confirm').disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    target = tmp_path / "draft-handlers.cjs"
    target.write_text(harness + handlers + checks)
    result = subprocess.run([node, str(target)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_narration_duration_metrics_and_advisories_do_not_block_confirm(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行内联浏览器事件处理器")
    html = (Path(__file__).parents[1] / "quickcut/web/index.html").read_text()
    assert "建议不超过" not in html
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    handlers = script[script.index("function reviewKey"):script.index("function drawResult")]
    harness = r'''
const assert=require('node:assert/strict');
let current='job',draftView,editPending=false,reviewDirty=false,editVersion=0,saveTimer,lastView='';
const nodes=[],elements=new Map(),storage=new Map();
function el(tag,text='',cls=''){
  const node={tag,textContent:text,className:cls,dataset:{},style:{},children:[],value:'',disabled:false,checked:false,
    append(...children){this.children.push(...children);},replaceChildren(){this.children=[];},
    setAttribute(name,value){this[name]=value;}};
  node.classList={
    contains:cls=>node.className.split(' ').includes(cls),
    toggle(cls,on){const set=new Set(node.className.split(' ').filter(Boolean));if(on)set.add(cls);else set.delete(cls);node.className=[...set].join(' ');},
    add(cls){this.toggle(cls,true);},remove(cls){this.toggle(cls,false);}
  };
  nodes.push(node);return node;
}
const $=id=>{if(!elements.has(id))elements.set(id,el('div'));return elements.get(id);};
const document={querySelector(selector){
  const [,name,value]=selector.match(/^\[data-([\w-]+)="([^"]+)"\]$/);
  const key=name.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());
  return nodes.find(node=>node.dataset[key]===value);
},querySelectorAll:()=>[]};
const localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
const setTimeout=()=>1,clearTimeout=()=>{};
let request;function toast(message){throw new Error(message);}async function poll(){}
function view(revision=0){return {draft_id:'frozen',review_revision:revision,beats:[{
  beat_id:'b',purpose:'开场',duration_budget_us:2000000,narration_max_chars:2,
  narration_draft:'我们沿着公园小路慢慢走，看看身边的花草。',
  narration_review:{character_count:18,estimated_duration_us:8000000,warnings:[],advisories:[{message:'配音后核对时长。'}]},
  review_warnings:[],shots:[],available_candidates:[]
}]};}
const field=name=>document.querySelector('[data-narration'+(name?'-'+name:'')+'="b"]');
function allText(node){return node.textContent+node.children.map(allText).join('');}
'''
    checks = r'''
(async()=>{
  drawReview(view());
  assert.equal(field('').maxLength,160);
  const content=allText($('beats'));
  assert.match(content,/编辑安全上限 160 字符/);
  assert.match(content,/按2\.5字\/秒\+0\.8秒粗估，不含标点；以配音实测为准/);
  assert.equal(field('metrics').textContent,'段落计划时长 2.0 秒 · 当前朗读字数 18 字 · 粗估时长 8.0 秒');
  assert.equal(field('advisory').textContent,'时长待实测：配音后核对时长。');
  assert.equal(field('advisory').classList.contains('hidden'),false);
  assert.equal(field('warning').classList.contains('hidden'),true);
  assert.equal($('confirm').disabled,false);

  // 软提示超过旧粗估时仍发送完整旁白。
  let sent;
  $('approved').checked=true;$('reviewer').value='测试确认者';
  request=async(url,options)=>{assert.ok(url.endsWith('/confirm'));sent=JSON.parse(options.body);return {};};
  await $('confirm').onclick();
  assert.equal(sent.approved_text_by_beat.b,field('').value);
  assert.equal(sent.confirmed,true);

  // 编辑时即时更新字数，排除标点和空白，并清除旧审阅结果。
  field('').value='枝条，轻轻摇动。 123 A';
  reviewChanged({target:field('')});
  assert.match(field('metrics').textContent,/当前朗读字数 10 字 · 粗估时长 4.8 秒/);
  assert.equal(field('warning').textContent,'正在检查修改后的旁白…');
  assert.equal(field('advisory').classList.contains('hidden'),true);
  assert.equal($('approved').checked,false);

  // 自动保存回包可同时包含阻断与软提示，分别展示。
  const checked=view(1);
  checked.beats[0].narration_review={character_count:10,estimated_duration_us:4800000,
    warnings:[{message:'请检查完整句。'}],advisories:[{message:'时长需要配音核对。'}]};
  checked.beats[0].review_warnings=checked.beats[0].narration_review.warnings;
  request=async()=>({draft:checked});
  await saveReview();
  assert.equal(field('warning').textContent,'这句需要修改：请检查完整句。');
  assert.equal(field('advisory').textContent,'时长待实测：时长需要配音核对。');
  assert.equal(field('warning').classList.contains('hidden'),false);
  assert.equal(field('advisory').classList.contains('hidden'),false);

  // 本地恢复重新计数，不使用服务器旧值或旧警告。
  field('').value='微风轻轻吹过。';rememberReview();
  field('').value='旧内容';restoreReview();
  assert.match(field('metrics').textContent,/当前朗读字数 6 字 · 粗估时长 3.2 秒/);
  assert.equal(field('warning').textContent,'正在检查修改后的旁白…');
  assert.equal(field('advisory').classList.contains('hidden'),true);
  checked.review_revision=2;
  checked.beats[0].narration_review={character_count:6,estimated_duration_us:3200000,warnings:[],advisories:[]};
  checked.beats[0].review_warnings=[];
  await saveReview();
  assert.equal(field('warning').classList.contains('hidden'),true);
  assert.equal(field('advisory').classList.contains('hidden'),true);
  assert.equal($('confirm').disabled,false);

  field('').value='， 。  ';reviewChanged({target:field('')});
  assert.match(field('metrics').textContent,/当前朗读字数 0 字 · 粗估时长 0.0 秒/);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    target = tmp_path / "narration-review.cjs"
    target.write_text(harness + handlers + checks)
    result = subprocess.run([node, str(target)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
