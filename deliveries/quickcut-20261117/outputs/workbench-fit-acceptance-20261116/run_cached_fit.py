"""Acceptance: copy real approved inputs, prohibit inference, exercise cached PCM + atempo."""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from quickcut import integration, voice_package
from quickcut.indextts2 import IndexTTS2Backend
from quickcut.media import digest, resolve_uri, write_json
from quickcut.planning import PlanResult
from quickcut.planning.contracts import ref
from quickcut.retiming import content_hash, verify_voice_media, validate_voice_fit_receipt
from quickcut.timeline_preview import _validate


def read(path):
    return json.loads(Path(path).read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--measured', default='voice-measured-r1.json')
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    source, dest = args.source.resolve(), args.destination.resolve()
    if source == dest or source in dest.parents or dest in source.parents:
        raise ValueError('Acceptance root must be independent of the primary job')
    dest.mkdir(parents=True, exist_ok=False)
    source_reads = {}
    def copy(relative):
        src, target = resolve_uri(source, relative), resolve_uri(dest, relative)
        before = digest(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        assert digest(target) == before == digest(src)
        source_reads[relative] = before
    for name in ('brief.json', 'asset-manifest.json', 'candidate-set.json', 'retrieval-request.json',
                 'story-plan.json', 'alignment.json', 'planning-run.json', 'boundaries.json', 'approval.json', args.measured):
        copy(name)
    measured = read(dest / args.measured)
    story = read(dest / 'story-plan.json')
    assert measured['inputs'] == [ref(story)] and measured['mode'] == 'measure'
    assert measured['fixtures_only'] is False and measured['synthesis_profile']['engine'] == 'indextts2'
    for asset in measured['assets']:
        copy(asset['uri'])
    config = read(args.config)
    for setting in [config, *config.get('cue_overrides', {}).values()]:
        for key in ('model_dir', 'reference_audio'):
            if key in setting:
                path = Path(setting[key]).expanduser()
                setting[key] = str(path if path.is_absolute() else REPO / path)
    backend = IndexTTS2Backend(config)  # validates paths/version only; model stays None
    for cue in measured['cues']:
        cue_config = {**config, **config.get('cue_overrides', {}).get(cue['cue_id'], {})}
        _, profile = backend.cue_settings(cue_config, story.get('narration_language', 'zh-CN'))
        key = voice_package.fingerprint({'text': cue['text'], 'profile': profile, 'trim_version': 1})
        assert key == cue['cache_key'], 'Configuration/profile changed; refusing any new synthesis'
        for suffix in ('wav', 'json'):
            copy(f'audio/cache/{key}.{suffix}')
        assert read(dest / f'audio/cache/{key}.json')['content_hash'] == digest(dest / f'audio/cache/{key}.wav')
    assert backend.model is None
    verify_voice_media(measured, dest)
    manifest = read(dest / 'asset-manifest.json')
    segments = {s['segment_id']: s for s in manifest['segments']}
    selected_assets = {segments[s['segment_id']]['asset_id'] for b in story['beats'] for s in b['shots']}
    for asset in manifest['assets']:
        if asset['asset_id'] in selected_assets:
            copy(asset['uri'])
            assert digest(dest / asset['uri']) == asset['content_hash']
    result = PlanResult([], read(dest / 'retrieval-request.json'), story,
                        read(dest / 'alignment.json'), read(dest / 'planning-run.json'))
    boundaries = read(dest / 'boundaries.json')
    # Choose an existing single-shot, single-cue beat. Only its acceptance-copy
    # boundary is shortened; approved text and all measured facts stay unchanged.
    beat = next(b for b in reversed(story['beats']) if len(b['shots']) == len(b['cue_ids']) == 1)
    cue = next(c for c in measured['cues'] if c['cue_id'] == beat['cue_ids'][0])
    desired_tempo = 1.08
    target_us = round(cue['raw_duration_us'] / desired_tempo) + cue['sentence_pause_us']
    assert 0 < target_us < cue['duration_us'] and desired_tempo <= config.get('max_tempo', 1.15)
    edit = next(e for e in boundaries['edits'] if e['beat_id'] == beat['beat_id'] and e['shot_index'] == 0)
    old_range = deepcopy(edit['source_range_us'])
    assert target_us <= old_range['end'] - old_range['begin']
    edit['source_range_us']['end'] = old_range['begin'] + target_us
    edit.update(reviewer='Codex cache-only acceptance, not a new human approval',
                reason='Deliberately shorten this isolated acceptance copy to exercise real atempo fitting; main job unchanged')
    write_json(dest / 'boundaries-fit-acceptance.json', boundaries)
    write_json(dest / 'voice-config.json', config)
    guard_calls, ffmpeg_calls, observed = [], [], []
    def forbid_inference(self, *values, **kwargs):
        guard_calls.append('blocked')
        raise RuntimeError('CACHE_ONLY_ACCEPTANCE: neural synthesis is prohibited')
    IndexTTS2Backend.synthesize = forbid_inference
    original_run = voice_package.run
    def logged_run(command):
        if '-af' in command:
            ffmpeg_calls.append(list(command))
        return original_run(command)
    voice_package.run = logged_run
    previous = measured
    def observer(record):
        observed.append(deepcopy(record))
        write_json(dest / f"fit-round-{record['iteration']}.json", record)
        write_json(dest / f"pending-fit-{record['iteration']}.json", record['pending_timeline'])
        write_json(dest / f"fit-request-{record['iteration']}.json", record['request'])
    def fit(request):
        nonlocal previous
        reply = voice_package.build_voice_package(story, dest, config=config, fixture=False,
            mode='fit', request=request, revision=request['voice_revision'], previous=previous)
        write_json(dest / f"voice-fit-r{reply['revision']}.json", reply)
        assert all(c.get('cache_hit') is True for c in reply['cues']), 'Cache miss is not an accepted test'
        previous = reply
        return reply
    report = {'source_root': str(source), 'scope': 'Real IndexTTS measured PCM reused from cache, real FFmpeg atempo, isolated D/E/F-contract acceptance',
        'source_measured_ref': ref(measured), 'source_measured_hash': content_hash(measured),
        'approved_story_ref': ref(story), 'approved_story_hash': content_hash(story),
        'boundary_change': {'beat_id': beat['beat_id'], 'cue_id': cue['cue_id'], 'before': old_range,
                            'after': edit['source_range_us'], 'requested_tempo_approx': desired_tempo},
        'fixtures_only': False, 'neural_inference_permitted': False, 'listening_acceptance': 'not_run'}
    try:
        built = integration.complete_voice_timeline(result, measured, brief=read(dest / 'brief.json'), manifest=manifest,
            candidate_set=read(dest / 'candidate-set.json'), output={'width':1280, 'height':720, 'fps':{'numerator':30,'denominator':1}, 'sample_rate':48000},
            project_root=dest, expected_story_revision=story['revision'], boundaries=boundaries,
            timeline_document_id='timeline-cache-fit-acceptance', timeline_revision=1,
            fit_provider=fit, fit_observer=observer, max_iterations=2)
        assert not guard_calls and ffmpeg_calls and built.fit_rounds
        assert any(c['tempo_ratio'] > 1 for c in built.voice_package['cues'])
        assert built.story_plan == story and measured == read(dest / args.measured)
        verify_voice_media(built.voice_package, dest)
        validate_voice_fit_receipt(built.timeline, built.voice_package, manifest=manifest)
        _validate(built.timeline, manifest, built.voice_package)
        write_json(dest / 'voice-package.json', built.voice_package)
        write_json(dest / 'timeline.json', built.timeline)
        write_json(dest / 'fit-rounds.json', built.fit_rounds)
        report.update(status='passed', fit_round_count=len(built.fit_rounds), f_contract_validation='passed',
            cues=[{k:c[k] for k in ('cue_id','text','cache_hit','duration_us','raw_duration_us','speech_end_us','sentence_pause_us','duration_budget_us','tempo_ratio','fit_status')} for c in built.voice_package['cues']],
            accepted_timeline_ref=ref(built.timeline), accepted_timeline_hash=content_hash(built.timeline))
    except Exception as exc:
        report.update(status='failed', code=getattr(exc,'code',None), message=str(exc),
                      fit_rounds=getattr(exc,'fit_rounds',[]))
        raise
    finally:
        report.update(neural_inference_attempts=len(guard_calls), actual_atempo_commands=ffmpeg_calls,
                      source_file_hashes=source_reads,
                      source_files_changed_during_acceptance=[name for name,sha in source_reads.items() if digest(resolve_uri(source,name)) != sha],
                      torch_imported='torch' in sys.modules)
        write_json(dest.parent / 'report.json', report)
    print(json.dumps({'status':report['status'],'fit_rounds':report['fit_round_count'],
                      'atempo_commands':len(ffmpeg_calls),'neural_inference_attempts':len(guard_calls),
                      'report':str(dest.parent/'report.json')}, ensure_ascii=False))

if __name__ == '__main__':
    main()
