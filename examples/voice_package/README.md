# E Module Delivery Examples

See [E-local-voice.md](../../docs/team/E-local-voice.md) for usage and handoff details.

- `story.json`: three handwritten Mandarin sentences; the third deliberately has a one-second budget.
- `config.json`: system voice settings used for the checked-in samples.
- `indextts2.config.json`: local IndexTTS2 settings for a speaker reference WAV. See the handoff guide for setup and the run command.
- `real/voice_package.json`: initial real Mandarin measurements; `real/audio/narration/` contains independent playable WAVs.
- `real/voice_package-r2.json` and `story-r2.json`: only the first sentence changes; other WAVs are reused.
- `fit-request.json` and `real/voice_package-fit.json`: D's exact request references and bounded fitting that preserves the final word.
- `fixture/`: test-tone outputs, with `fixtures_only: true`; these are not Mandarin speech.
- `evidence/`: media hash/timing verification, test logs, raw local ASR responses, and environment records.

Every URI is relative to the corresponding `real/` or `fixture/` project root. A few MB of actual media are included; no personal media directories are needed. Runtime cache WAVs contain speech after boundary-silence removal and are excluded from Git by repository policy. Delivered narration WAVs include sentence-end padding and optional bounded tempo adjustment. The first run on a new machine builds its cache; later sentence edits can reuse it.

Documentation and descriptive metadata are English. Mandarin scripts/subtitles, raw ASR evidence, and exact installed voice identifiers retain their original language to match the actual audio.

Reproduce success, overrun, revision, fitting, and deliberate failure:

```bash
python scripts/demo_voice_package.py --fixture --root outputs/e-fixture
python scripts/demo_voice_package.py --root outputs/e-real
```
