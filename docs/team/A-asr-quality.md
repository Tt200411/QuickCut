# A: ASR configuration, review flags, and evaluation

## Changes

Ingestion exposes model selection, language, beam size, CPU threads, worker count, VAD, previous-text conditioning, initial prompts, and vocabulary hints. It defaults to two threads, one worker, beam size 5, and independent decoding of previous text. The existing single-source adapter keeps its defaults.

The adapter keeps language detection and segment/word diagnostics. Ingestion converts diagnostic timestamps to source microseconds and stores them in the transcript sidecar. A diagnostic VAD failure produces a warning without discarding recognized text.

Transcript `status: success` means execution succeeded. New `review.status: needs_review` explicitly preserves the need for human review. Heuristic log-probability, no-speech, compression, and speech-tail checks are advisory and uncalibrated. High-confidence substitutions can escape these flags; an empty flagged-segment list is not an accuracy approval. B/C should retain this distinction when consuming transcript evidence.

`python -m quickcut.asr_eval` compares a sidecar with a human-verified reference. It binds references to source hashes, records interval selection, and reports substitutions, deletions, insertions, and CER. Optional OpenCC normalization avoids counting traditional/simplified spelling as a recognition difference. Unverified published scripts require an explicit flag and retain an unverified label.

## Validation, 2026-11-13

- Targeted suite: 40 passed.
- Full Windows suite: 102 passed, 12 failed. Failures remain the same baseline macOS font and Windows subtitle-path issues documented for the initial ingestion delivery.
- A local 45-second Mandarin sample was taken from Yangmingshan National Park's Lengshuikeng observation-platform video. Source and licensing: [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:%E9%99%BD%E6%98%8E%E5%B1%B1%E5%9C%8B%E5%AE%B6%E5%85%AC%E5%9C%92_%E5%86%B7%E6%B0%B4%E5%9D%91%E8%A7%80%E6%99%AF%E5%8F%B0.webm), CC BY 3.0. Footage, models, and transcripts remain outside the Git index.
- The user checked the first two spoken sentences against the audio. The fixed evaluation interval is source seconds 4.5-12.4, containing 33 normalized reference characters.
- Baseline base model: 1 substitution, no deletions or insertions, CER 1/33 (3.03%).
- Small model with thread limits, independent decoding, language hint, and a place-name vocabulary: also 1 substitution, no deletions or insertions, CER 1/33 (3.03%). The hypothesis still substitutes the homophone in the phrase meaning "viewpoint". The reference sentence was not used as a model prompt.
- Both model runs use Mandarin and beam size 5. Reports distinguish the baseline's segment-midpoint selection from the new word-midpoint selection; the selected first two sentences contain the same 33 characters after normalization.
- Known place names in the vocabulary improved some later names on inspection, while other spelling and wording errors remain. The remainder of the video has no user-verified reference and no numeric accuracy claim.
- The latest run returns `needs_review` and a speech-tail review recommendation. The test clip ends within an ongoing explanation, so that flag is not proof of a dropped final sentence.

## Acceptance boundary

Configuration, diagnostics, explicit review state, cache isolation, and reference evaluation are implemented. The real test does not demonstrate an improvement on the verified short interval. It does not establish whole-video or general Mandarin recognition accuracy. Larger verified samples and comparison with a Mandarin-focused backend remain follow-up work.

Run details, reference text, model hashes, and hypotheses are saved locally in `outputs/asr-quality-review/`. Usage and field semantics are in [the ingestion reference](../ingestion.md).
