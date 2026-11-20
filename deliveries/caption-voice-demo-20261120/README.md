# Caption and narration demo

This 90-second cut keeps the same ten source excerpts, scene order, narration text
and music as the previous edit. It demonstrates medium-sized wide captions and
0.9x narration tempo with the speaker's pitch preserved. Short captions stay on
one line; longer ones wrap at pauses. The opening street violin remains free of
added music.

- `demo.mp4`: finished video with captions.
- `editable-project.zip`: exported timeline, separate audio tracks, captions and
  the media required by the editable project.
- `caption-preview.jpg`: an actual frame from the finished video.
- `verification.json` and `SHA256SUMS.txt`: validation results and file hashes.
- `MUSIC_CREDITS.txt`: attribution to retain when publishing the video.

The video and ZIP use Git LFS. After cloning, run `git lfs pull` to download them.
For a direct download on GitHub, use the file's Download button to retrieve the
LFS object rather than saving its pointer text.

To open the editable project, extract the ZIP, completely close Jianying, and run
the repository's installer from the repository root:

```sh
python scripts/install_jianying_draft.py <extracted-package> --draft-root "<your Jianying draft folder>" --name Travel-Caption-Demo
```

The installer regenerates media paths for the destination machine. The archive's
draft paths are relative placeholders. The export uses the repository's existing
Jianying adapter; native open/edit/save/reopen has not yet been verified.

Video decoding, caption placement, package verification and ZIP integrity checks
passed. This is a presentation revision of an existing cut. Full automatic scene
selection still needs a separate check. The original reference recording is not
included; the demo contains the narration made from it with the speaker's consent.
