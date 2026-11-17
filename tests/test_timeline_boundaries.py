from copy import deepcopy

import pytest

from examples.team_contracts.demo_d import OUTPUT, load_case
from quickcut.timeline import TimelineError, _ref, compile_timeline


@pytest.fixture
def docs():
    return {d['kind']: d for d in load_case('normal')['documents']}


def boundary(docs, *, begin=3_500_000, end=9_000_000):
    return {'story_plan': _ref(docs['StoryPlan']), 'asset_manifest': _ref(docs['AssetManifest']),
        'edits': [{'beat_id': 'beat-open', 'shot_index': 0, 'segment_id': 'seg-packing',
            'source_range_us': {'begin': begin, 'end': end}, 'method': 'manual',
            'reviewer': 'test-reviewer', 'reason': 'Confirm this exact action interval'}]}


def compile_(docs, bounds):
    return compile_timeline(docs['Brief'], docs['AssetManifest'], docs['StoryPlan'], docs['VoicePackage'],
                            output=OUTPUT, boundaries=bounds)


def test_manual_bounds_are_recorded_and_keep_original_audio_aligned(docs):
    bounds = boundary(docs)
    before = deepcopy((docs, bounds))
    timeline = compile_(docs, bounds)
    assert (docs, bounds) == before
    picture, original = timeline['tracks'][:2]
    assert timeline['duration_us'] == 17_500_000
    assert picture['clips'][0]['source_range_us'] == {'begin': 3_500_000, 'end': 9_000_000}
    for key in ('source_range_us', 'timeline_range_us'):
        assert picture['clips'][0][key] == original['clips'][0][key]
    decision = timeline['boundary_decisions'][0]
    assert decision['method'] == 'manual' and decision['reviewer'] == 'test-reviewer'
    assert decision['clip_id'] == picture['clips'][0]['clip_id']
    assert decision['source_range_us'] == bounds['edits'][0]['source_range_us']
    assert all(d['method'] == 'segment_prefix_budget' for d in timeline['boundary_decisions'][1:])


def test_repeated_segment_selections_have_independent_bounds(docs):
    beat = docs['StoryPlan']['beats'][0]
    shot = beat['shots'][0]
    beat['shots'] = [{**shot, 'suggested_duration_us': 3_000_000}, {**shot, 'suggested_duration_us': 3_000_000}]
    bounds = boundary(docs, begin=3_000_000, end=6_000_000)
    bounds['edits'].append({**deepcopy(bounds['edits'][0]), 'shot_index': 1,
                           'source_range_us': {'begin': 6_000_000, 'end': 9_000_000}})
    timeline = compile_(docs, bounds)
    first, second = timeline['tracks'][0]['clips'][:2]
    assert first['segment_id'] == second['segment_id']
    assert first['source_range_us'] != second['source_range_us']
    assert [d['shot_index'] for d in timeline['boundary_decisions'][:2]] == [0, 1]


def test_manual_end_never_expands_to_fit_speech(docs):
    with pytest.raises(TimelineError) as exc:
        compile_(docs, boundary(docs, begin=3_000_000, end=8_000_000))
    assert exc.value.code == 'VOICE_NEEDS_RETIME'


@pytest.mark.parametrize('damage', ['story_revision', 'asset_revision', 'bool_revision', 'wrong_segment',
    'duplicate', 'outside', 'bool_index', 'reviewer', 'reason', 'method'])
def test_invalid_or_stale_manual_decision_is_rejected(docs, damage):
    bounds = boundary(docs)
    edit = bounds['edits'][0]
    if damage == 'story_revision':
        bounds['story_plan']['revision'] += 1
    elif damage == 'asset_revision':
        bounds['asset_manifest']['revision'] += 1
    elif damage == 'bool_revision':
        bounds['story_plan']['revision'] = True
    elif damage == 'wrong_segment':
        edit['segment_id'] = 'seg-walk'
    elif damage == 'duplicate':
        bounds['edits'].append(deepcopy(edit))
    elif damage == 'outside':
        edit['source_range_us']['begin'] = 2_999_999
    elif damage == 'bool_index':
        edit['shot_index'] = False
    elif damage in ('reviewer', 'reason'):
        edit[damage] = ' '
    else:
        edit['method'] = 'model_action_detection'
    with pytest.raises(TimelineError):
        compile_(docs, bounds)
