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
    assert "完整句级字幕，显示层会自动换行" in html
    assert "确认，下一步" in html
    assert "确认并生成成片" not in html
    assert "逐词对齐" not in html
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
  const matched=String(selector).match(/^\[data-([\w-]+)="([^"]+)"\]$/);
  if(!matched)return null;
  const [,name,value]=matched;
  const key=name.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());
  return nodes.find(node=>node.dataset[key]===value)||null;
},querySelectorAll:()=>[]};
const localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
const setTimeout=()=>1,clearTimeout=()=>{};
let request,latest={status:'needs_review'},openedStep=null;
function openStep(index){openedStep=index;}
function toast(message){throw new Error(message);}async function poll(){}
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
  assert.equal(field('metrics').textContent,'段落计划时长 2.0 秒 · 当前朗读字数 18 字 · 粗估时长 8.0 秒 · 尚未实测');
  assert.match(content,/字数不是字幕单行上限/);
  assert.match(content,/粗估仅供参考，不阻断确认/);
  assert.equal(field('advisory').textContent,'时长待实测：配音后核对时长。');
  assert.equal(field('advisory').classList.contains('hidden'),false);
  assert.equal(field('warning').classList.contains('hidden'),true);
  assert.equal($('confirm').disabled,false);

  // 软提示不阻断确认；确认只进入配音，完整旁白在配音步生成时发送。
  let sent,posts=0;
  $('approved').checked=true;
  request=async(url,options)=>{posts++;assert.ok(url.endsWith('/confirm'));sent=JSON.parse(options.body);return {};};
  await $('confirm').onclick();
  assert.equal(posts,0);
  assert.equal(openedStep,3);
  await $('startVoiceover').onclick();
  assert.equal(posts,1);
  assert.equal(sent.approved_text_by_beat.b,field('').value);
  assert.equal(sent.confirmed,true);
  assert.equal(sent.reviewer,'本机用户');
  assert.equal(sent.voice,'male');

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


def test_health_separates_generation_and_editor_and_clears_failed_results(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 执行健康检查处理器")
    html = (Path(__file__).parents[1] / "quickcut/web/index.html").read_text()
    assert html.index('id="healthBlockers"') < html.index('id="healthDetails"')
    assert "white-space:pre-wrap;overflow-wrap:anywhere" in html
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    handlers = script[script.index("async function health()"):script.index("function referenceNote")]
    harness = r'''
const assert=require('node:assert/strict');
const el=(tag,text='',className='')=>({tag,textContent:text,className,children:[],open:false,disabled:false,
  append(...children){this.children.push(...children);},replaceChildren(){this.children=[];}});
const elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,el('div'));return elements.get(id);};
let request;
function result(generationReady,editorReady){return {
  ready:generationReady&&editorReady,generation_ready:generationReady,editor_ready:editorReady,
  scope:'只检查文件与服务，不执行推理。',checked_at:'2026-11-19T10:00:00+00:00',
  checks:[{stage:'E',name:'配音权重与参考音频',ready:generationReady,detail:'Git LFS 指针\n/path/<音色>.wav'},
          {stage:'editor',name:'剪映专业版',ready:editorReady,detail:editorReady?'6.1.0':'未找到'}]
};}
'''
    checks = r'''
(async()=>{
  for(const [generation,editor] of [[true,false],[false,true],[false,false],[true,true]]){
    $('healthDetails').open=false;
    request=async()=>result(generation,editor);await health();
    assert.equal($('healthLabel').textContent,generation?'生成依赖检查通过':'生成依赖待处理');
    assert.equal($('healthDetails').open,!generation);
    assert.match($('healthScope').textContent,/不执行推理/);
    assert.match($('healthCheckedAt').textContent,/2026-11-19/);
    const blockers=$('healthBlockers').textContent;
    assert.ok(generation?blockers.includes('生成阻塞项：无'):blockers.includes('生成阻塞项：\n• 配音权重与参考音频'));
    assert.ok(!blockers.includes('• 剪映'));
    if(!editor)assert.match(blockers,/不阻止生成视频和工程包/);
    const detail=$('modelList').children[0].children[2];
    assert.equal(detail.textContent,'Git LFS 指针\n/path/<音色>.wav');
    assert.equal(detail.className,'model-detail');
    assert.equal(detail.innerHTML,undefined,'详情必须以明文呈现');
  }
  // 兼容旧接口时，不能把包含编辑器的 ready 当作生成门禁。
  request=async()=>{const data=result(true,false);delete data.generation_ready;delete data.editor_ready;return data;};
  await health();assert.equal($('healthLabel').textContent,'生成依赖检查通过');
  // 请求期间即清除旧值；失败后不得保留已通过的检查或时间。
  let reject;request=()=>new Promise((resolve,fail)=>reject=fail);
  const pending=health();
  assert.equal($('refreshHealth').disabled,true);
  assert.equal($('modelList').children.length,0);
  assert.equal($('healthCheckedAt').textContent,'');
  reject(new Error('服务不可达'));await pending;
  assert.equal($('healthLabel').textContent,'检查失败');
  assert.equal($('healthLabel').className,'pending');
  assert.equal($('modelList').children.length,0);
  assert.equal($('healthScope').textContent,'');
  assert.equal($('healthCheckedAt').textContent,'');
  assert.match($('healthBlockers').textContent,/状态未知：服务不可达/);
  assert.equal($('healthDetails').open,true);
  assert.equal($('refreshHealth').disabled,false);
  request=async()=>result(true,true);await health();
  assert.equal($('healthLabel').className,'ok');
  assert.equal($('modelList').children.length,2);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    target = tmp_path / "health-handlers.cjs"
    target.write_text(harness + handlers + checks)
    result = subprocess.run([node, str(target)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def _run_navigation_handlers(tmp_path, checks):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the inline browser handlers")
    html = (Path(__file__).parents[1] / "quickcut/web/index.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    handlers = script[script.index("function selectJob"):script.index("function busy")]
    handlers += script[script.index("function stageIndex"):script.index("function statusBox")]
    handlers += script[script.index("function renderNarrationMetrics"):script.index("async function changeSelection")]
    harness = r'''
const assert=require('node:assert/strict');
class Element {
  constructor(tag,text){this.tag=tag;this.textContent=text;this.children=[];this.dataset={};this.style={};this.classes=new Set();this.attributes={};this.paused=true;this.pauseCalls=0;
    this.classList={add:key=>this.classes.add(key),remove:key=>this.classes.delete(key),contains:key=>this.classes.has(key),toggle:(key,value)=>{if(value===undefined)value=!this.classes.has(key);if(value)this.classes.add(key);else this.classes.delete(key);}};
  }
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=[...children];}
  setAttribute(name,value){this.attributes[name]=value;}
  removeAttribute(name){delete this.attributes[name];}
  querySelectorAll(tag){return this.children.flatMap(child=>[...(tag==='*'||child.tag===tag?[child]:[]),...child.querySelectorAll(tag)]);}
  pause(){this.paused=true;this.pauseCalls++;}
}
const elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
const el=(tag,text,cls='')=>{const node=new Element(tag,text);for(const name of cls.split(' ').filter(Boolean))node.classList.add(name);return node;};
const document={querySelector(selector){
  const [,name,value]=selector.match(/^\[data-([\w-]+)="([^"]+)"\]$/);
  const key=name.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());
  return $('beats').querySelectorAll('*').find(node=>node.dataset[key]===value)||null;
}};
$('steps').append(...Array.from({length:5},()=>new Element('button')));
$('review').append($('beats'));
let current='a',latest,preferredIndex=null,navigationVersion=0,lastView='',draftView=null,reviewDirty=false,editVersion=0,editPending=false,saveTimer;
const storage=new Map(),messages=[],polled=[];
const localStorage={setItem:(key,value)=>storage.set(key,value)};
const clearTimeout=()=>{},rememberReview=()=>{},restoreReview=()=>{},setEditBusy=()=>{},changeSelection=()=>{},reviewChanged=()=>{};
const toast=value=>messages.push(value);
let request;
async function poll(){polled.push(current);if(latest)applyView(latest);}
function completed(id){return {job_id:id,status:'completed',stage:'completed',downloads:{video:id+'.mp4'},result:{}};}
function draft(ids){return {beats:ids.map((beat_id,i)=>({beat_id,purpose:'第'+(i+1)+'个原句',duration_budget_us:10000000,shots:[],available_candidates:[],narration_max_chars:30,narration_draft:'完整旁白。'}))};}
'''
    target = tmp_path / "navigation-handlers.cjs"
    target.write_text(harness + handlers + "\n(async()=>{\n" + checks + "\n})().catch(error=>{console.error(error);process.exitCode=1;});\n")
    result = subprocess.run([node, str(target)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_delayed_return_to_review_cannot_change_a_newer_navigation(tmp_path):
    _run_navigation_handlers(tmp_path, r'''
for(const switchBack of [false,true]){
  selectJob('a');latest=completed('a');applyView(latest);
  let resolve;request=()=>new Promise(done=>resolve=done);
  const oldRequest=openStep(2);
  selectJob('b');latest=completed('b');applyView(latest);
  if(switchBack){selectJob('a');latest=completed('a');applyView(latest);}
  const calls=polled.length;
  resolve({});await oldRequest;
  assert.equal(preferredIndex,null,'An old return-review response must not override the selected job view');
  assert.equal(polled.length,calls,'The stale navigation must not trigger a poll for the newly selected job');
  assert.equal($('result').classList.contains('hidden'),false);
}
// A later step click in the same job wins over an earlier pending navigation.
selectJob('a');latest=completed('a');
let resolve;request=()=>new Promise(done=>resolve=done);
const oldStep=openStep(2);await openStep(4);resolve({});await oldStep;
assert.equal(preferredIndex,4);
assert.equal($('result').classList.contains('hidden'),false);
// Errors from an abandoned job must not appear as errors of the current one.
let reject;request=()=>new Promise((done,fail)=>reject=fail);
const failedStep=openStep(2);selectJob('b');latest=completed('b');
reject(new Error('old job failed'));await failedStep;
assert.deepEqual(messages,[]);
''')


def test_navigation_pauses_hidden_previews_but_keeps_the_visible_video(tmp_path):
    _run_navigation_handlers(tmp_path, r'''
const data=draft(['beat-01']);
data.beats[0].shots=[{shot_index:0,candidate_id:'c1',segment_id:'s1',available_range_us:{begin:0,end:10000000},source_range_us:{begin:0,end:5000000},media_url:'/source.mp4'}];
data.beats[0].available_candidates=[{candidate_id:'c1',label:'素材1'}];
drawReview(data);
const source=$('review').querySelectorAll('video')[0],final=$('finalVideo');
assert.ok(source,'Exercise the source video created by the actual review renderer');
source.paused=false;final.paused=false;showPanel('review');
assert.equal(source.paused,false,'Showing the review must not interrupt its active preview');
assert.equal(final.paused,true);
for(const panel of ['materials','voiceStep','result','empty']){
  source.paused=false;final.paused=false;showPanel(panel);
  assert.equal(source.paused,true,'Hidden source audio must stop when leaving review');
  assert.equal(final.paused,panel!=='result','Only the visible final player may keep playing');
}
source.paused=false;final.paused=false;selectJob('b');
assert.equal(source.paused,true,'Switching jobs must immediately stop the old source preview');
assert.equal(final.paused,true);
''')


def test_review_titles_follow_beat_identity_for_one_three_and_four_clauses(tmp_path):
    _run_navigation_handlers(tmp_path, r'''
function titles(ids){drawReview(draft(ids));return $('beats').children.map(card=>card.children[0].children[0].textContent);}
assert.deepEqual(titles(['beat-open','beat-middle','beat-close']),['开场','展开','收尾']);
for(const count of [1,3,4]){
  const ids=Array.from({length:count},(_,i)=>'beat-'+String(i+1).padStart(2,'0'));
  assert.deepEqual(titles(ids),ids.map((_,i)=>'第 '+(i+1)+' 段 · 第'+(i+1)+'个原句'));
}
''')
