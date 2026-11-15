"""Verify E's real media package independently; does not arrange D's timeline."""
import argparse
import hashlib
import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quickcut.voice_package import reference, safe_file, validate_story


def verify(story, package, root, allow_fixture=False):
    validate_story(story)
    assert package['schema_version'] == '0.1.0' and package['kind'] == 'VoicePackage'
    assert package['owner'] == 'E' and package['timing_authority'] == 'D'
    assert package['project_id'] == story['project_id']
    assert reference(story) in package['inputs'], 'stale script revision'
    assert allow_fixture or not package['fixtures_only'], 'fixture is not production speech'
    assets = {a['asset_id']:a for a in package['assets']}
    assert len(assets) == len(package['assets'])
    cues = {c['cue_id']:c for c in package['cues']}
    assert len(cues) == len(package['cues'])
    assert set(cues) == {c['cue_id'] for c in story['narration_cues']}
    report = []
    for original in story['narration_cues']:
        c = cues[original['cue_id']]
        assert c['text'] == original['text'] and c['script_input'] == reference(story)
        if c['fit_status'] == 'failed':
            assert any(c['cue_id'] in e['affected_ids'] for e in package['errors'])
            report.append({'cue_id':c['cue_id'], 'status':'failed', 'code':c['error_code']})
            continue
        a = assets[c['asset_id']]
        path = safe_file(Path(root), a['uri'])
        assert a['origin'] == 'generated'
        assert a['content_hash'] == 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
        with wave.open(str(path), 'rb') as wav:
            assert (wav.getnchannels(),wav.getsampwidth(),wav.getframerate()) == (1,2,48000)
            us = round(wav.getnframes()*1000000/wav.getframerate())
            assert us == a['duration_us'] == c['duration_us']
        assert ''.join(s['text'] for s in c['local_subtitles']) == original['text']
        for sub in c['local_subtitles']:
            r = sub['range_us']
            assert type(r['begin']) is int and type(r['end']) is int
            assert 0 <= r['begin'] < r['end'] <= us
        assert c['needs_retime'] == (us > c['duration_budget_us'])
        assert c['fit_status'] == ('needs_retime' if c['needs_retime'] else 'fits')
        assert 1 <= c['tempo_ratio'] <= 1.25
        assert original['text'] in safe_file(Path(root), c['srt_uri']).read_text()
        report.append(dict(cue_id=c['cue_id'],duration_us=us,fit_status=c['fit_status'],hash_verified=True))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--story', type=Path, required=True)
    p.add_argument('--package', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--allow-fixture', action='store_true')
    p.add_argument('--report', type=Path)
    a = p.parse_args()
    report = verify(json.loads(a.story.read_text()),json.loads(a.package.read_text()),a.root,a.allow_fixture)
    output = json.dumps({'media_validation':'passed', 'cues':report},ensure_ascii=False,indent=2)
    if a.report:
        a.report.write_text(output+'\n')
    print(output)


if __name__ == '__main__':
    main()
