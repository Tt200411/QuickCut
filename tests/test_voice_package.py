import array
import copy
import json
from pathlib import Path

import pytest

from quickcut import voice_package as v


@pytest.fixture
def story():
    return json.loads(Path('examples/voice_package/story.json').read_text())


def build(story, root, **kwargs):
    return v.build_voice_package(story, root, fixture=True, **kwargs)


def test_local_timing_and_no_mutation(story, tmp_path):
    original = copy.deepcopy(story)
    p = build(story, tmp_path)
    assert story == original
    assert p['fixtures_only'] is True
    assert p['status'] == 'needs_retime'
    assert p['inputs'] == [v.reference(story)]
    for c, a in zip(p['cues'], p['assets']):
        assert c['duration_us'] == v.duration(v.read_pcm(tmp_path / a['uri']))
        assert a['content_hash'] == v.digest(tmp_path / a['uri'])
        assert c['local_subtitles'][0]['text'] == c['text']
        assert c['local_subtitles'][0]['range_us']['end'] <= c['duration_us']
    assert p['cues'][-1]['needs_retime']


def test_boundary_trim_preserves_internal_pause():
    samples = array.array('h', [0]*4800 + [500]*4800 + [0]*9600 + [500]*4800 + [0]*4800)
    result = v.trim_boundaries(samples)
    assert len(result) == 4800 + 9600 + 4800 + 3840
    assert result[6720:16320] == array.array('h', [0]*9600)
    with pytest.raises(v.VoiceError, match='silence'):
        v.trim_boundaries(array.array('h', [0]*100))


def test_change_one_cue_reuses_others(story, tmp_path):
    first = build(story, tmp_path)
    story['revision'] += 1
    story['narration_cues'][1]['text'] = '停下来，听听风声。'
    second = build(story, tmp_path, previous=first, revision=2)
    assert second['invalidated_cue_ids'] == ['cue-02']
    assert [c['cache_hit'] for c in second['cues']] == [True, False, True]
    assert second['alignment_invalidated']
    assert second['inputs'][0]['revision'] == 2


def test_corrupt_cache_is_regenerated(story, tmp_path):
    first = build(story, tmp_path)
    cache = tmp_path / 'audio/cache' / (first['cues'][0]['cache_key']+'.wav')
    cache.write_bytes(b'corrupt')
    second = build(story, tmp_path)
    assert not second['cues'][0]['cache_hit']
    assert second['cues'][1]['cache_hit']


def test_failure_distinguished_from_silence(story, tmp_path, monkeypatch):
    def broken(text, path, profile):
        if text == story['narration_cues'][0]['text']:
            raise OSError('engine failed')
        v.write_pcm(path, array.array('h', [0]*48000))
    monkeypatch.setattr(v, 'synthesize', broken)
    p = build(story, tmp_path)
    assert p['status'] == 'failed'
    assert [e['code'] for e in p['errors']] == ['TTS_FAILED', 'NO_SPEECH', 'NO_SPEECH']
    assert not p['assets']


def test_partial_failure_keeps_good_audio(story, tmp_path, monkeypatch):
    original = v.synthesize
    def one_failure(text, path, profile):
        if text == story['narration_cues'][1]['text']:
            raise OSError('failed cue')
        return original(text, path, profile)
    monkeypatch.setattr(v, 'synthesize', one_failure)
    p = build(story, tmp_path)
    assert p['status'] == 'partial'
    assert len(p['assets']) == 2
    assert p['errors'][0]['affected_ids'] == ['cue-02']


def test_bounded_fit_never_crops_long_audio(story, tmp_path):
    story['narration_cues'][0]['duration_budget_us'] = 1100000
    p = build(story, tmp_path, mode='fit')
    assert 1 < p['cues'][0]['tempo_ratio'] <= 1.15
    assert p['cues'][0]['fit_status'] == 'fits'
    long = p['cues'][-1]
    assert long['tempo_ratio'] == 1
    assert long['needs_retime']
    assert long['duration_us'] == 1180000


def test_revision_and_interval_request(story, tmp_path):
    request = dict(project_id=story['project_id'], story_plan=v.reference(story),
                   timeline=dict(kind='EditTimeline', document_id='edit-1', revision=3),
                   iteration=1, cue_ranges={'cue-01':dict(begin=200000, end=2000000)})
    p = build(story, tmp_path, mode='fit', request=request)
    assert p['request'] == request
    assert p['inputs'][1]['revision'] == 3
    assert p['cues'][0]['requested_timeline_range_us'] == request['cue_ranges']['cue-01']
    assert p['cues'][0]['local_subtitles'][0]['range_us']['begin'] == 0
    request['story_plan']['revision'] = 99
    with pytest.raises(v.VoiceError) as e:
        build(story, tmp_path, request=request)
    assert e.value.code == 'STALE_INPUT_REVISION'


def test_retry_limit(story, tmp_path):
    req = dict(project_id=story['project_id'], story_plan=v.reference(story),
               timeline=dict(kind='EditTimeline', document_id='t', revision=1), iteration=3)
    with pytest.raises(v.VoiceError) as e:
        build(story, tmp_path, mode='fit', request=req)
    assert e.value.code == 'VOICE_NEEDS_RETIME'


@pytest.mark.parametrize('change', [lambda s:s.update(revision=True),
    lambda s:s['narration_cues'][0].update(duration_budget_us=1.5),
    lambda s:s['narration_cues'][0].update(text=''),
    lambda s:s['narration_cues'][1].update(cue_id='cue-01')])
def test_invalid_contract(story, tmp_path, change):
    change(story)
    with pytest.raises(v.VoiceError):
        build(story, tmp_path)


def test_reject_path_escape(tmp_path):
    with pytest.raises(v.VoiceError):
        v.safe_file(tmp_path, '../escape.wav')
    (tmp_path/'link').symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(v.VoiceError):
        v.safe_file(tmp_path, 'link/escape.wav')


def test_real_profile_capability_validation(monkeypatch):
    monkeypatch.setattr(v, 'capabilities', lambda: dict(engine='say',version='test',voices=[dict(voice='Chinese',language='zh-CN')]))
    assert v.select_profile({}, 'zh-CN', False)['voice'] == 'Chinese'
    with pytest.raises(v.VoiceError):
        v.select_profile({'voice':'Missing'}, 'zh-CN', False)
    with pytest.raises(v.VoiceError):
        v.select_profile({'emotion':'happy'}, 'zh-CN', False)


def test_single_voice_change_invalidates_one_cue(story, tmp_path, monkeypatch):
    monkeypatch.setattr(v, 'capabilities', lambda:dict(engine='say',version='test',voices=[
        dict(voice='A',language='zh-CN'),dict(voice='B',language='zh-CN')]))
    def tone(text, path, profile):
        v.write_pcm(path, array.array('h',[1000]*48000))
    monkeypatch.setattr(v,'synthesize',tone)
    first = v.build_voice_package(story,tmp_path,config={'voice':'A'})
    second = v.build_voice_package(story,tmp_path,previous=first,revision=2,
        config={'voice':'A','cue_overrides':{'cue-02':{'voice':'B'}}})
    assert second['invalidated_cue_ids'] == ['cue-02']
    assert [c['cache_hit'] for c in second['cues']] == [True,False,True]


def test_real_media_verifier_rejects_tampering(story, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('verify', 'scripts/verify_voice_package.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = build(story,tmp_path)
    module.verify(story,package,tmp_path,True)
    with pytest.raises(AssertionError, match='fixture'):
        module.verify(story,package,tmp_path)
    package['assets'][0]['content_hash'] = 'sha256:wrong'
    with pytest.raises(AssertionError):
        module.verify(story,package,tmp_path,True)


def test_cli_preserves_existing_output(story, tmp_path):
    import subprocess
    path = tmp_path/'story.json'
    path.write_text(json.dumps(story))
    cmd = [__import__('sys').executable,'-m','quickcut.voice_package','--fixture',
           '--story',str(path),'--root',str(tmp_path)]
    first = subprocess.run(cmd,capture_output=True,text=True)
    assert first.returncode == 2  # deliberate long cue
    output = tmp_path/'voice_package-r1.json'
    content = output.read_bytes()
    second = subprocess.run(cmd,capture_output=True,text=True)
    assert 'OUTPUT_EXISTS' in second.stderr
    assert output.read_bytes() == content


def test_indextts2_uses_reference_in_cache_identity(story, tmp_path, monkeypatch):
    import hashlib
    import wave

    from quickcut.indextts2 import IndexTTS2Backend

    def init(backend, config):
        backend.runtime_version = "2.0.0"
        backend.model_revision = config["model_revision"]
        backend.device = "mps"

    calls = []

    def synthesize(backend, text, output, reference, seed):
        calls.append((text, reference, seed))
        with wave.open(str(output), "wb") as wav:
            wav.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            wav.writeframes(array.array("h", [1500] * 22050).tobytes())

    monkeypatch.setattr(IndexTTS2Backend, "__init__", init)
    monkeypatch.setattr(IndexTTS2Backend, "synthesize", synthesize)
    speaker = tmp_path / "speaker.wav"
    speaker.write_bytes(b"reference version one")
    config = {
        "engine": "indextts2", "model_revision": "test-checkpoint",
        "reference_audio": str(speaker), "seed": 42,
    }
    root = tmp_path / "output"
    first = v.build_voice_package(story, root, config=config)
    assert [c["fit_status"] for c in first["cues"]] == ["fits", "fits", "needs_retime"]
    assert first["synthesis_profile"]["reference_sha256"] == hashlib.sha256(speaker.read_bytes()).hexdigest()
    assert str(speaker) not in json.dumps(first)
    assert len(calls) == 3
    assert all(call[1:] == (speaker, 42) for call in calls)
    assert all(a["audio"]["sample_rate"] == 48000 for a in first["assets"])

    second = v.build_voice_package(story, root, config=config, previous=first, revision=2)
    assert all(c["cache_hit"] for c in second["cues"])
    assert len(calls) == 3

    speaker.write_bytes(b"reference version two")
    third = v.build_voice_package(story, root, config=config, previous=second, revision=3)
    assert third["invalidated_cue_ids"] == ["cue-01", "cue-02", "cue-03"]
    assert len(calls) == 6
