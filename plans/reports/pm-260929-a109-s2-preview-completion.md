## Plan Complete: Restore chronological S2 preview lookup a109

### Summary

- Proved that the project already contained 74 S2 cues, while binary-search
  lookup failed because S2 was appended after all S1 cues instead of being
  ordered by timestamp.
- Ordered the subtitle model chronologically before building its playhead
  indexes. Existing a108 project data now renders S2 in Preview without OCR,
  translation or export reruns.
- Compared full-frame scene evidence against finalized S1 text, time and image
  geometry. Replaying the real OCR evidence reduced 74 proposed S2 cues to 13:
  11 accepted scene labels and two review cues, with 61 duplicated S1 readings
  removed.
- Preserved a genuine scene label when it repeats the spoken text at another
  image position.

### Verification

- Real snapshot lookup returned `Loạn Tinh Hải`, `Hàn Lập`, `Hải Vương Thú`
  and `Tịch Tà Thần Lôi` at their expected playheads.
- Focused OCR/UI regression suite, broad OCR/UI/export/translation suite and
  full pytest passed with the existing single opt-in skip.
- Ruff, compileall, QML lint and `git diff --check` passed.
- Release manifest: 2,871 files, zero missing files and zero hash mismatches.
- Packaged Windows x64 Job Manager/runtime diagnostics and media-import smoke
  passed and shut down cleanly.

### Artifact

`E:\Codex\2026-07-21\create-a-scheduled-task-called-weekday\outputs\DUCAI-STUDIO-2.0.0a109-win64`

### Known limitation

Paddle scene-text recognition remains probabilistic. The repair prevents an
existing valid S2 cue from disappearing in Preview and removes proven S1
duplicates; it does not promise recognition of every stylized label in every
video.
