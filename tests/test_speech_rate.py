"""Delivery-rate controls keep pitch, measured timing and cache provenance."""
import array
import hashlib
import math
import wave

import pytest

from quickcut.indextts2 import IndexTTS2Backend, _adjust_delivery
from quickcut.voice_package import fingerprint


@pytest.mark.parametrize("value", [True, "0.9", [], float('nan'), float('inf'), .5, 2])
def test_invalid_delivery_rate_is_rejected_before_model_loading(tmp_path, value):
    with pytest.raises(ValueError, match='delivery_rate'):
        IndexTTS2Backend({'model_dir':str(tmp_path),'model_revision':'fixed','delivery_rate':value})


def test_delivery_rate_changes_cache_identity_without_changing_reference(tmp_path):
    path=tmp_path/'reference.wav'
    path.write_bytes(b'reference unchanged; no inference in this profile test')
    backend=object.__new__(IndexTTS2Backend)
    backend.device='cpu'
    backend.model_revision='fixed'
    backend.runtime_version='test'
    profiles=[]
    for rate in (1.0,.9):
        backend.delivery_rate=rate
        _,profile=backend.cue_settings({'reference_audio':str(path)},'zh-CN')
        profiles.append(profile)
    assert profiles[0]['reference_sha256']==profiles[1]['reference_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert fingerprint(profiles[0])!=fingerprint(profiles[1])
    assert 'delivery_rate' not in profiles[0]


@pytest.mark.parametrize('tempo',[.8,.9,1.1])
def test_real_tempo_filter_preserves_pitch_and_measured_duration(tmp_path,tempo):
    path=tmp_path/'speech.wav'
    sample_rate=24000
    samples=array.array('h',(round(18000*math.sin(2*math.pi*440*i/sample_rate)) for i in range(2*sample_rate)))
    with wave.open(str(path),'wb') as wav:
        wav.setparams((1,2,sample_rate,0,'NONE','not compressed'))
        wav.writeframes(samples.tobytes())
    _adjust_delivery(path,tempo)
    with wave.open(str(path),'rb') as wav:
        seconds=wav.getnframes()/wav.getframerate()
        adjusted=array.array('h',wav.readframes(wav.getnframes()))
    assert abs(seconds-2/tempo)<.08
    crossings=sum(a<=0<b for a,b in zip(adjusted,adjusted[1:]))
    assert abs(crossings/seconds-440)<5
