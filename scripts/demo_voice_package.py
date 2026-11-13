"""Reproduce E's three Chinese cues, partial failure, fitting and local revision."""
import argparse
import copy
import json
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quickcut import voice_package as v
from verify_voice_package import verify


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fixture', action='store_true')
    p.add_argument('--root', type=Path, required=True)
    a = p.parse_args()
    story = json.loads(Path('examples/voice_package/story.json').read_text())
    config = json.loads(Path('examples/voice_package/config.json').read_text())
    save(a.root/'story.json',story)
    first = v.build_voice_package(story,a.root,config=config,fixture=a.fixture)
    save(a.root/'voice-r1.json',first)
    assert first['cues'][-1]['needs_retime']
    changed = copy.deepcopy(story)
    changed['revision'] = 2
    changed['narration_cues'][0]['text'] = '我们出发了。'
    second = v.build_voice_package(changed,a.root,config=config,fixture=a.fixture,previous=first,revision=2)
    save(a.root/'story-r2.json',changed)
    save(a.root/'voice-r2.json',second)
    assert second['invalidated_cue_ids'] == ['cue-01']
    assert second['cues'][1]['cache_hit'] and second['cues'][2]['cache_hit']
    request = dict(project_id=story['project_id'],story_plan=v.reference(story),
        timeline=dict(kind='EditTimeline',document_id='edit-e-demo',revision=4),iteration=1,
        cue_ranges={'cue-01':dict(begin=0,end=850000 if not a.fixture else 1100000)})
    fit = v.build_voice_package(story,a.root,config=config,fixture=a.fixture,mode='fit',request=request,revision=3)
    save(a.root/'fit-request.json',request)
    save(a.root/'voice-fit.json',fit)
    assert fit['cues'][0]['fit_status'] == 'fits'
    with patch.object(v,'synthesize',side_effect=OSError('deliberate engine failure')):
        failure = v.build_voice_package(story,a.root/'failure',fixture=True)
    save(a.root/'failure/voice.json',failure)
    assert failure['status'] == 'failed'
    save(a.root/'verification.json',dict(initial=verify(story,first,a.root,a.fixture),
        updated=verify(changed,second,a.root,a.fixture),fitted=verify(story,fit,a.root,a.fixture),
        revision_test='only cue-01 changed; cue-02/cue-03 reused',failure_test='TTS_FAILED returned'))
    print('PASS: three cues, bounded fit, revision reuse, full failure, media hashes and local subtitle timing')


if __name__ == '__main__':
    main()
