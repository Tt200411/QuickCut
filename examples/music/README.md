# Automatic background music

The catalog contains three instrumental tracks by Scott Buckley: **Reverie**,
**Childhood**, and **A Kind Of Hope**. They are available under **CC BY 4.0**.
They are free to use, including in commercial videos, with attribution.
Read the [composer's terms](https://www.scottbuckley.com.au/library/using-this-music/).
For YouTube, the composer requires credits in the video description.

Install the pinned downloads from the official HTTPS URLs:

```sh
python scripts/setup_music.py
python scripts/setup_music.py --verify
```

Downloads and the installed catalog are stored under `outputs/music-library`.
Raw MP3 files are not included in Git. The installer verifies every SHA256 and
refuses to replace a mismatched existing file. The three downloads total about
27 MiB. Set `QUICKCUT_MUSIC_CATALOG` to another installed catalog if needed.

In the workbench, enable **Automatic background music** (自动选择背景音乐).
The option is off by default and stays unavailable until the catalog is installed.
Selection matches local catalog
tags against the editing brief and records its matched tags or default fallback.
The starter catalog covers calm piano and strings; it does not cover every
genre or perform beat detection. No model, account, subscription, or runtime
network request is required for selection.

Music is normalized, fitted to the picture duration, faded at the ends, and
attenuated around the actual narration intervals. The prepared stereo PCM is
used by both the preview and a separate editable music track in the Jianying
package. The volume changes are baked into this music resource; native editable
volume-envelope keyframes are not claimed. Existing D/E timing and the original
four timeline tracks remain intact.

The export includes `MUSIC_CREDITS.txt`. Keep that attribution when publishing.
The package records the source URL, license URL, and prepared track hash.
The project also saves the selection evidence and mix recipe in
`music-selection.json`. Jobs with music disabled keep their existing behavior.

Source pages, checked on 2026-11-20:

- [Reverie](https://www.scottbuckley.com.au/library/reverie/)
- [Childhood](https://www.scottbuckley.com.au/library/childhood/)
- [A Kind Of Hope](https://www.scottbuckley.com.au/library/a-kind-of-hope/)

Native opening, editing, saving, and reopening in Windows Jianying still require
an observed editor acceptance run; a generated package alone does not prove it.
