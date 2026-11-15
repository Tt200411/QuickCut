# A: Batch ingestion example

`sources.json` lists six input slots; media files are not included. Place the videos under the repository's `inputs/` directory and update the paths, origins, and `provenance_note` values before running:

```bash
python -m quickcut.ingest --sources examples/ingest/sources.json --project-root outputs/my-project
```

Input paths are relative to the JSON file. Output URIs are relative to the project root. Use a stable `source_key` for later revisions of the same asset, and a different key for each distinct asset.

Without input footage, run `python -m scripts.smoke_ingest`. It generates three shorts, one 65-second video, and two B-roll test entries under `outputs/ingest-smoke/inputs`, with results under `outputs/ingest-smoke/project`. Patterns, tones, and silence exercise ingestion and origin handling; they do not validate real speech or visual semantics.

`outputs/` is Git-ignored. Generated media, manifests, and caches do not need to be committed. See the [ingestion reference](../../docs/ingestion.md) for fields and cache rules.
