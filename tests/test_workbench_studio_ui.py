"""Execute the merged studio's actual deck, language and music handlers."""
from pathlib import Path
import shutil
import subprocess

import pytest


HTML = Path(__file__).parents[1] / "quickcut/web/index.html"


DOM = r'''
const assert=require('node:assert/strict');
class Element {
  constructor(tag,text='',cls='') {
    this.tag=tag;this.children=[];this.parent=null;this.dataset={};this.style={};this.attributes={};
    this.value='';this.disabled=false;this.checked=false;this.paused=true;this.pauseCalls=0;
    this.readyState=1;this.currentTime=0;this.clientWidth=480;this.ownText=text??'';
    this.classes=new Set(cls.split(' ').filter(Boolean));
    this.classList={contains:c=>this.classes.has(c),add:c=>this.classes.add(c),remove:c=>this.classes.delete(c),
      toggle:(c,on)=>{if(on===undefined)on=!this.classes.has(c);if(on)this.classes.add(c);else this.classes.delete(c);}};
  }
  get textContent(){return this.ownText+this.children.map(c=>c.textContent).join('');}
  set textContent(value){this.ownText=String(value??'');this.children=[];}
  append(...nodes){for(const node of nodes){if(node.parent)node.parent.children=node.parent.children.filter(n=>n!==node);node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes){for(const node of this.children)node.parent=null;this.children=[];this.ownText='';this.append(...nodes);}
  setAttribute(name,value){this.attributes[name]=String(value);}
  removeAttribute(name){delete this.attributes[name];}
  getAttribute(name){return this.attributes[name]??null;}
  toggleAttribute(name,on){if(on)this.setAttribute(name,'');else this.removeAttribute(name);}
  get childNodes(){return this.children;}
  querySelectorAll(selector){return descendants(this).filter(node=>selector.split(',').some(s=>matches(node,s.trim())));}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  closest(selector){return selector.split(',').some(s=>matches(this,s.trim()))?this:this.parent?.closest(selector)||null;}
  pause(){this.paused=true;this.pauseCalls++;}
}
class TextNode {
  constructor(value){this.nodeType=3;this.nodeValue=value;this.parent=null;this.children=[];}
  get textContent(){return this.nodeValue;}
}
function descendants(root){return root.children.flatMap(n=>[n,...descendants(n)]);}
function matches(node,selector){
  if(node.nodeType===3)return false;
  if(selector==='*')return true;
  const space=selector.indexOf(' ');
  if(space>=0){const parent=selector.slice(0,space);return matches(node,selector.slice(space+1))&&!!node.parent?.closest(parent);}
  if(selector.startsWith('#'))return node.id===selector.slice(1);
  if(selector.startsWith('.'))return node.classList.contains(selector.slice(1));
  if(selector.includes('>'))return false;
  if(selector.startsWith('['))return [...selector.matchAll(/\[data-([\w-]+)="([^"]*)"\]/g)].every(([,key,value])=>String(node.dataset[key.replace(/-([a-z])/g,(_,c)=>c.toUpperCase())])===value)&&selector.includes('="');
  return node.tag===selector;
}
const elements=new Map(),storage=new Map();
const $=id=>{if(!elements.has(id)){const node=new Element('div');node.id=id;elements.set(id,node);}return elements.get(id);};
const allNodes=()=>[...new Set([...elements.values()].flatMap(n=>[n,...descendants(n)]))];
const document={body:{},documentElement:{},createTextNode:value=>new TextNode(value),
  querySelectorAll:selector=>allNodes().filter(n=>selector.split(',').some(s=>matches(n,s.trim()))),
  querySelector:selector=>document.querySelectorAll(selector)[0]||null,
  createTreeWalker(){const nodes=allNodes();let i=0;return {nextNode:()=>nodes[i++]||null};}};
const Node={TEXT_NODE:3},NodeFilter={SHOW_ELEMENT:1,SHOW_TEXT:4};
const localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
const setTimeout=()=>1,clearTimeout=()=>{},requestAnimationFrame=callback=>callback();
'''


def run(tmp_path, code, checks):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the studio handlers")
    target = tmp_path / "studio.cjs"
    target.write_text(DOM + code + "\n" + checks)
    result = subprocess.run([node, str(target)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def script():
    return HTML.read_text().split("<script>", 1)[1].split("</script>", 1)[0]


def test_shot_deck_preserves_all_confirmation_inputs_and_playback(tmp_path):
    source = script()
    handlers = source[source.index("function showDeck"):source.index("function drawResult")]
    handlers += source[source.index("function stageIndex"):source.index("function paintSteps")]
    setup = r'''
const el=(tag,text,cls='')=>new Element(tag,text,cls);
const tr=(parts,...values)=>parts.reduce((s,p,i)=>s+(i?values[i-1]:'')+p,'');
const setAttr=(node,key,value)=>node.setAttribute(key,value);
let draftView,current='job',editPending=false,reviewDirty=false,editVersion=0,saveTimer,lastView='',deckAt=0;
let request=async()=>({});function toast(message){throw Error(message);}async function poll(){}
'''
    checks = r'''
const shot=(i,cid)=>({candidate_id:cid,shot_index:i,segment_id:'s'+cid,origin:'user',media_url:cid+'.mp4',
  available_range_us:{begin:0,end:10000000},source_range_us:{begin:1500000,end:8000000}});
const beat=(id,shots)=>({beat_id:id,purpose:'画面',duration_budget_us:10000000,narration_draft:'完整的中文旁白。',
  shots,available_candidates:shots.map(s=>({candidate_id:s.candidate_id,label:s.candidate_id})),review_warnings:[]});
drawReview({draft_id:'draft',review_revision:0,beats:[beat('one',[shot(0,'a'),shot(1,'b')]),beat('two',[shot(0,'c')])]});
assert.equal($('beats').children.length,3,'Every selected shot has its own page');
assert.ok($('beats').children.every(page=>page.querySelectorAll('video').length===1));
assert.equal(document.querySelectorAll('[data-narration="one"]').length,1,'Shots share their beat narration editor');
assert.equal(document.querySelectorAll('[data-narration="two"]').length,1);
showDeck(0);
const first=$('beats').children[0].querySelector('video');
assert.equal(first.currentTime,1.5);
first.paused=false;first.currentTime=4.2;
showDeck(0);showDeck(0);
assert.equal(first.currentTime,4.2,'Polling/resizing the same page must not rewind playback');
assert.equal(first.paused,false);
showDeck(1);
assert.equal(first.paused,true);
assert.equal($('beats').children[1].querySelector('video').currentTime,1.5);
assert.equal($('beats').children[0].getAttribute('inert'),'');
assert.equal($('beats').children[1].getAttribute('inert'),null);
assert.equal($('narrationDock').children[0].classList.contains('hidden'),false);
assert.equal($('narrationDock').children[1].classList.contains('hidden'),true);
document.querySelector('[data-narration="one"]').value='同一段只有这份已编辑旁白。';
showDeck(2);
assert.equal($('narrationDock').children[0].classList.contains('hidden'),true);
assert.equal($('narrationDock').children[1].classList.contains('hidden'),false);
const saved=reviewSnapshot();
assert.deepEqual(saved.selection_overrides,{one:['a','b'],two:['c']});
assert.equal(saved.draft_boundaries.length,3,'Hidden pages still submit every trim');
assert.equal(saved.draft_text_edits.one,'同一段只有这份已编辑旁白。');
assert.equal(saved.draft_text_edits.two,'完整的中文旁白。');
assert.equal(saved.confirmed,undefined);
assert.equal(stepEnabled(3,{status:'completed',result:{},downloads:{video:'done.mp4'}}),false);
assert.equal(stepEnabled(4,{status:'completed',downloads:{video:'done.mp4'}}),true);
'''
    run(tmp_path, setup + handlers, checks)


def test_music_toggle_and_credits_survive_studio_language_switch(tmp_path):
    source = script()
    helpers = source[source.index("const english"):source.index("const $=id")]
    handlers = source[source.index("function drawResult"):source.index("$('openEditor').onclick")]
    checks = r'''
language='en';
$('autoMusic').checked=true;configureMusic({});
assert.equal($('autoMusic').disabled,true);assert.equal($('autoMusic').checked,false);
assert.match($('musicNote').textContent,/not installed/);
configureMusic({music:{available:true}});
assert.equal($('autoMusic').disabled,false);assert.equal($('autoMusic').checked,false,'Music stays opt-in');
const data={downloads:{video:'/film.mp4',package:'/project.zip',report:'/report.json'},result:{duration_us:30000000,
  music:{title:'Test title',attribution:'Artist — CC BY 4.0 https://example.com/license'}}};
drawResult(data);
assert.equal($('resultMusic').classList.contains('hidden'),false);
assert.match($('resultMusic').textContent,/Test title/);
assert.match($('resultMusic').textContent,/Artist — CC BY 4.0 https:\/\/example.com\/license/);
assert.match($('resultDuration').textContent,/five-track/);
language='zh';applyLanguage();
assert.match($('resultDuration').textContent,/五轨/);
assert.match($('resultMusic').textContent,/发布时请保留署名/);
delete data.result.music;drawResult(data);applyLanguage();
assert.equal($('resultMusic').classList.contains('hidden'),true);
assert.equal($('resultMusic').textContent,'','An earlier music credit must not leak into a silent result');
assert.match($('resultDuration').textContent,/四轨/);
'''
    run(tmp_path, helpers + handlers, checks)


def test_english_refresh_does_not_restore_stale_health_status(tmp_path):
    source = script()
    helpers = source[source.index("const english"):source.index("const $=id")]
    checks = r'''
language='en';
$('healthLabel').textContent='生成依赖检查通过';applyLanguage();
assert.equal($('healthLabel').textContent,'Generation checks passed');
$('healthLabel').textContent='检查失败';applyLanguage();
assert.equal($('healthLabel').textContent,'Check failed','A new failed health result must replace the old pass');
language='zh';applyLanguage();assert.equal($('healthLabel').textContent,'检查失败');
language='en';applyLanguage();assert.equal($('healthLabel').textContent,'Check failed');
$('healthLabel').textContent='New English diagnostic';applyLanguage();
assert.equal($('healthLabel').textContent,'New English diagnostic');
language='zh';applyLanguage();assert.equal($('healthLabel').textContent,'New English diagnostic');
$('deckTitle').textContent='第 1 段 · 草木';language='en';applyLanguage();applyLanguage();
language='zh';applyLanguage();assert.equal($('deckTitle').textContent,'第 1 段 · 草木','Repeated English rendering must preserve the Chinese source');
'''
    run(tmp_path, helpers, checks)
