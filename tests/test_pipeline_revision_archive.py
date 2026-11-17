"""Revision traces with real D/E fixture processing; no neural or editor execution."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from quickcut import integration, production_pipeline as pipeline, voice_package
from quickcut.media import write_json
from quickcut.planning import load_bundle, plan, read_json
from quickcut.retiming import content_hash
from quickcut.workbench import Jobs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    root = tmp_path / 'jobs' / 'aabbccddeeff'
    root.mkdir(parents=True)
    _, docs = load_bundle(ROOT / 'examples/team_contracts/bundle.json')
    candidates = read_json(ROOT / 'examples/team_contracts/c/candidate_set.json')
    planned = plan(docs['Brief'], docs['AssetManifest'], candidates,
                   approved_cues=read_json(ROOT / 'examples/team_contracts/c/approved_cues.json'))
    planned.story_plan['narration_cues'][0]['duration_budget_us'] = 1_100_000
    for name, value in [('brief.json', docs['Brief']), ('asset-manifest.json', docs['AssetManifest']),
        ('candidate-set.json', candidates), ('retrieval-request.json', planned.retrieval_request),
        ('draft.json', {'status': 'needs_review'}), ('settings.json', {'subtitle_position': 'top'}),
        ('job.json', {'job_id': root.name, 'status': 'rendering', 'stage': 'D/E', 'render_attempt': 1})]:
        write_json(root / name, value)
    config = tmp_path / 'voice-config.json'
    write_json(config, {})
    runtime = pipeline.Runtime(voice_config=str(config))
    controls = {'failure': None, 'calls': []}
    def confirm(draft, **kwargs):
        result = deepcopy(planned)
        result.story_plan['revision'] = kwargs['plan_revision']
        return result
    monkeypatch.setattr('quickcut.planning.production.confirm_draft', confirm)
    monkeypatch.setattr(pipeline, 'unload_qwen', lambda runtime: None)
    def worker(runtime, action, data, project_root, result_name, **kwargs):
        assert action == 'voice'
        assert Path(data['story']).parent.name.startswith('r')
        request = read_json(data['request']) if data.get('request') else None
        controls['calls'].append((result_name, deepcopy(data)))
        if request:
            archive = Path(data['request']).parent
            assert read_json(archive / 'pending-fit-1.json')['status'] == 'pending_voice_fit'
            assert read_json(archive / 'fit-rounds.json')[0]['status'] == 'requested'
            if controls['failure']:
                raise pipeline.PipelineError(controls['failure'], 'deliberate child interruption', stage='E')
        voice = voice_package.build_voice_package(read_json(data['story']), project_root,
            fixture=True, mode=data.get('mode', 'measure'), revision=data['revision'], request=request,
            previous=read_json(data['previous']) if data.get('previous') else None)
        write_json(Path(project_root) / (result_name + '.json'), voice)
        return voice
    monkeypatch.setattr(pipeline, 'worker', worker)
    def export(timeline, manifest, voice, project_root, package, **kwargs):
        assert timeline['render_policy']['subtitle_position'] == 'top'
        assert timeline['voice_fit']['request']['request_id'] == voice['request']['request_id']
        package.mkdir()
        (package / 'trace-test-only.txt').write_text('Not an editor export; archive regression only.')
        return {'kind': 'ExportManifest', 'document_id': 'export-test-' + str(timeline['revision']),
                'revision': 1, 'status': 'generated'}
    monkeypatch.setattr('quickcut.export.export_project', export)
    return root, runtime, controls


def confirmation(revision):
    return {'revision': revision, 'approved_text_by_beat': {}, 'reviewer': 'test-reviewer',
            'reviewed_at': '2026-11-16T00:00:00Z', 'confirmation_kind': 'assistant_review', 'boundaries': []}


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_failed_attempt_trace_survives_successful_retry_and_current_alias_moves(scenario):
    root, runtime, controls = scenario
    controls['failure'] = 'TTS_FAILED'
    with pytest.raises(integration.IntegrationError) as exc:
        pipeline.render(root, confirmation(1), runtime, lambda *args: None)
    assert exc.value.code == 'VOICE_PROVIDER_FAILED'
    first = root / 'revisions/r1'
    assert read_json(first / 'attempt.json')['status'] == 'failed'
    assert read_json(first / 'fit-rounds.json')[0]['status'] == 'provider_failed'
    assert read_json(first / 'error.json')['code'] == 'VOICE_PROVIDER_FAILED'
    assert (first / 'voice-failed.json').is_file()
    before = snapshot(first)
    controls['failure'] = None
    report = pipeline.render(root, confirmation(2), runtime, lambda *args: None)
    assert snapshot(first) == before
    second = root / 'revisions/r2'
    assert read_json(second / 'attempt.json')['status'] == 'completed'
    assert report['revision_archive'] == 'revisions/r2'
    assert report['approval'] == str(second / 'approval.json')
    assert report['confirmation_kind'] == 'assistant_review' and 'human_approval' not in report
    assert read_json(root / 'approval.json')['revision'] == 2
    assert read_json(root / 'story-plan.json')['revision'] == 2
    assert read_json(first / 'story-plan.json')['revision'] == 1
    assert read_json(root / 'fit-request-1.json') == read_json(second / 'fit-request-1.json')
    assert read_json(first / 'fit-request-1.json')['request_id'] != read_json(second / 'fit-request-1.json')['request_id']
    assert read_json(second / 'fit-rounds.json')[0]['status'] == 'accepted'
    before_all = snapshot(root)
    with pytest.raises(pipeline.PipelineError) as conflict:
        pipeline.render(root, confirmation(2), runtime, lambda *args: None)
    assert conflict.value.code == 'REVISION_EXISTS'
    assert snapshot(root) == before_all


def test_presentation_policy_is_archived_without_rewriting_fit_receipt(scenario):
    root, runtime, _ = scenario
    pipeline.render(root, confirmation(1), runtime, lambda *args: None)
    archive = root / 'revisions/r1'
    before, after = read_json(archive / 'timeline-d.json'), read_json(archive / 'timeline.json')
    policy = read_json(archive / 'presentation-policy.json')
    assert policy['d_timeline_content_hash'] == content_hash(before)
    assert policy['final_timeline_content_hash'] == content_hash(after)
    assert before['voice_fit'] == after['voice_fit']
    assert 'subtitle_position' not in before['render_policy']
    assert after['render_policy'].pop('subtitle_position') == 'top'
    assert before == after


def test_cancelled_fit_archives_pending_and_jobs_marks_interrupted(scenario):
    root, runtime, controls = scenario
    controls['failure'] = 'CANCELLED'
    with pytest.raises(integration.IntegrationError) as exc:
        pipeline.render(root, confirmation(1), runtime, lambda *args: None)
    assert exc.value.code == 'CANCELLED'
    jobs = Jobs(root.parent, runtime)
    try:
        jobs.fail(root.name, exc.value)
        assert jobs.get(root.name)['status'] == 'interrupted'
        archive = root / 'revisions/r1'
        assert read_json(archive / 'attempt.json')['status'] == 'interrupted'
        assert read_json(archive / 'fit-rounds.json')[0]['status'] == 'cancelled'
        assert read_json(root / 'error.json')['code'] == 'CANCELLED'
    finally:
        jobs.close()
