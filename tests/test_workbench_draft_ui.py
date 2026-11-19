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


def _run_navigation_handlers(tmp_path, checks):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the inline browser handlers")
    html = (Path(__file__).parents[1] / "quickcut/web/index.html").read_text()
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    handlers = script[script.index("function selectJob"):script.index("function busy")]
    handlers += script[script.index("function stageIndex"):script.index("function statusBox")]
    handlers += script[script.index("function drawReview"):script.index("async function changeSelection")]
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
  querySelectorAll(tag){return this.children.flatMap(child=>[...(child.tag===tag?[child]:[]),...child.querySelectorAll(tag)]);}
  pause(){this.paused=true;this.pauseCalls++;}
}
const elements=new Map();
const $=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
const el=(tag,text)=>new Element(tag,text);
$('steps').append(...Array.from({length:5},()=>new Element('button')));
$('review').append($('beats'));
let current='a',latest,preferredIndex=null,navigationVersion=0,lastView='',draftView=null,reviewDirty=false,editVersion=0,editPending=false,saveTimer;
const storage=new Map(),messages=[],polled=[];
const localStorage={setItem:(key,value)=>storage.set(key,value)};
const clearTimeout=()=>{},rememberReview=()=>{},renderNarrationWarnings=()=>{},restoreReview=()=>{},setEditBusy=()=>{},changeSelection=()=>{},reviewChanged=()=>{};
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
