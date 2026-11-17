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
