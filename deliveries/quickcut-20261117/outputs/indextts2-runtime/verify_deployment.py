from pathlib import Path
import gc, hashlib, json, shutil, time
from quickcut.voice_package import build_voice_package
from quickcut.integration import compile_from_voice
from quickcut.planning import PlanResult
from quickcut.export import export_project
from scripts.verify_voice_package import verify

repo = Path(__file__).resolve().parents[2]
root = repo / 'outputs/indextts2-acceptance-20261115/countryside'
root.mkdir(parents=True, exist_ok=True)
inputs = ['brief.json','asset-manifest.json','candidate-set.json','retrieval-request.json','story-plan.json','alignment.json','approved-cues.json']
for name in inputs:
    if not (root / name).exists():
        shutil.copy2(repo / 'ABCDE' / name, root / name)
for name in ['media','derived']:
    if not (root/name).exists():
        shutil.copytree(repo / 'ABCDE' / name, root/name)
def read(name):
    return json.loads((root/name).read_text())
def save(name, data):
    (root/name).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
story = read('story-plan.json')
config = json.loads((repo/'examples/voice_package/indextts2.config.json').read_text())
report = {'scope':'Real IndexTTS2 E synthesis plus D/F on frozen ABCDE A/B/C inputs; A/B/C inference not rerun', 'model':'IndexTeam/IndexTTS-2', 'model_revision':config['model_revision'], 'reference':'User-approved project system-speech Mandarin sample; not original user test voice', 'input_sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in inputs}}
save('deployment-verification.json', report)
started = time.monotonic()
voice = build_voice_package(story, root, config=config)
save('voice-package-measure.json', voice)
report['inference_wall_seconds'] = round(time.monotonic()-started, 3)
report['measure_status'] = voice['status']
report['cues'] = [{'cue_id':c['cue_id'],'duration_us':c.get('duration_us'),'fit_status':c['fit_status'],'cache_hit':c.get('cache_hit')} for c in voice['cues']]
report['synthesis_profile'] = voice['synthesis_profile']
save('deployment-verification.json', report)
print('REAL INFERENCE',json.dumps(report, ensure_ascii=False),flush=True)
if voice['status'] == 'needs_retime':
    voice = build_voice_package(story,root,config=config,mode='fit',previous=voice,revision=2)
    save('voice-package-fit.json',voice)
if voice['status'] != 'ready':
    raise RuntimeError(json.dumps({'status':voice['status'],'errors':voice.get('errors')},ensure_ascii=False))
verify(story, voice, root)
save('voice-package.json',voice)
cache = build_voice_package(story,root,config=config,mode=voice['mode'],previous=voice,revision=voice['revision']+1)
save('voice-package-cache-check.json',cache)
report['cache_reuse_all_cues'] = all(c['cache_hit'] for c in cache['cues'])
assert report['cache_reuse_all_cues']
plan = PlanResult(outline=[],retrieval_request=read('retrieval-request.json'),story_plan=story,alignment=read('alignment.json'))
built = compile_from_voice(plan,voice,brief=read('brief.json'),manifest=read('asset-manifest.json'),candidate_set=read('candidate-set.json'),output={'width':1280,'height':720,'fps':{'numerator':30,'denominator':1},'sample_rate':48000},timeline_document_id='timeline-countryside-indextts2',timeline_revision=1)
save('timeline.json', built.timeline)
save('assets-union.json', {'assets':built.assets,'fixtures_only':False})
report['timeline_status'] = built.timeline['status']
gc.collect()
import torch
torch.mps.empty_cache()
result = export_project(built.timeline,read('asset-manifest.json'),voice,root,root.parent/'export',preview=True,name='QuickCut IndexTTS2 740dcaf')
report['export'] = result
report['status'] = 'passed'
save('deployment-verification.json', report)
print('DEPLOYMENT VERIFIED',json.dumps(report,ensure_ascii=False),flush=True)
