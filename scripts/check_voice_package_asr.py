"""Optional local Whisper evidence; approved subtitle text is never changed."""
import argparse
from dataclasses import asdict
import difflib
import json
from pathlib import Path
import re
import subprocess


def main():
    from faster_whisper import WhisperModel
    import numpy as np
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--package', type=Path, required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--model', default='base')
    a = p.parse_args()
    package = json.loads(a.package.read_text())
    assets = {x['asset_id']:x for x in package['assets']}
    model = WhisperModel(a.model, device='cpu', compute_type='int8')
    results = []
    compact = lambda s: re.sub(r'[^\u3400-\u9fffA-Za-z0-9]', '', s).lower()
    for cue in package['cues']:
        if cue['fit_status'] == 'failed':
            continue
        asset = assets[cue['asset_id']]
        pcm = subprocess.run(['ffmpeg','-v','error','-i',str(a.root/asset['uri']),'-ar','16000','-ac','1','-f','f32le','-'], check=True, capture_output=True).stdout
        segments, info = model.transcribe(np.frombuffer(pcm, dtype='<f4'), language='zh', beam_size=5,
            word_timestamps=True, vad_filter=False, condition_on_previous_text=False)
        raw = [asdict(s) for s in segments]
        words = [w for s in raw for w in s['words']]
        hypothesis = ''.join(s['text'] for s in raw)
        tail = compact(cue['text'])[-4:]
        recognized = compact(hypothesis)
        word_end = max((w['end'] for w in words),default=0)
        results.append(dict(cue_id=cue['cue_id'], approved_text=cue['text'],
            hypothesis=hypothesis, raw_segments=raw, duration_us=asset['duration_us'],
            character_similarity=difflib.SequenceMatcher(None,compact(cue['text']),recognized).ratio(),
            closing_text=tail, closing_text_found=tail in recognized,
            last_word_end_us=round(word_end*1e6),
            tail_gap_us=asset['duration_us']-round(word_end*1e6)))
    a.report.write_text(json.dumps(dict(model=a.model,device='cpu',compute_type='int8',
        inference='local', note='ASR is corroborating evidence, not a replacement script or naturalness rating',
        cues=results),ensure_ascii=False,indent=2)+'\n')
    for r in results:
        print(r['cue_id'],r['character_similarity'],r['closing_text_found'],r['tail_gap_us'])


if __name__ == '__main__':
    main()
