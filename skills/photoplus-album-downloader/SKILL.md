---
name: photoplus-album-downloader
description: Download PhotoPlus / 谱时图片直播 and Pailixiang / 拍立享 albums from a URL or activity ID; use to inspect metadata, filter date tabs, save JSON, or write caption/GPS metadata.
version: 1.0.0
dependencies: python>=3.10, requests, tqdm, piexif; optional exiftool for --gps-from-image
metadata:
  openclaw:
    requires:
      bins:
        - python3
    skillKey: photoplus-album-downloader
    homepage: https://github.com/helloene/live-album-downloader
---

# PhotoPlus Album Downloader

## Overview

Use the upstream Python project `helloene/live-album-downloader` to download images from public PhotoPlus or Pailixiang live albums. Always confirm the user has permission to download/store the album contents when the album is not clearly theirs.

## Quick Workflow

1. Extract the activity ID or album code from the user input. PhotoPlus IDs are numeric and usually appear in:

```text
https://live.photoplus.cn/live/12345678
https://live.photoplus.cn/live/pc/12345678/#/live
```

Pailixiang album codes usually appear in the `/album/a<code>` path.

2. Prefer the bundled wrapper because it accepts a PhotoPlus URL/ID or Pailixiang URL/code and can clone/download the upstream project if needed:

```bash
python3 /path/to/photoplus-album-downloader/scripts/download_photoplus_album.py \
  "https://live.photoplus.cn/live/12345678" \
  --workdir /path/to/output-root \
  --install-deps
```

```bash
python3 /path/to/photoplus-album-downloader/scripts/download_photoplus_album.py \
  "a<album-code>" \
  --workdir /path/to/output-root \
  --install-deps
```

3. If dependencies are already installed and the upstream repo is already present, call the upstream script directly:

```bash
python3 live_album_downloader.py --id 12345678
```

```bash
python3 live_album_downloader.py --id "a<album-code>"
```

4. Report the output folder. The upstream project writes PhotoPlus albums to `./PhotoPlus/<activity_id>/` and Pailixiang albums to `./Pailixiang/<album_code>/` from the command working directory, or the same source root with `--folder-name` when provided.

## Common Commands

Inspect album metadata and tab names before downloading:

```bash
python3 scripts/download_photoplus_album.py 12345678 --inspect --install-deps
```

Download only a date-like tab:

```bash
python3 scripts/download_photoplus_album.py 12345678 --tab 3.29 --folder-name "event-3.29"
```

Save metadata sidecars and preserve useful filenames:

```bash
python3 scripts/download_photoplus_album.py 12345678 \
  --save-metadata \
  --rename-template "{date}_{time}_{name}"
```

Write album title caption and GPS EXIF/IPTC metadata:

```bash
python3 scripts/download_photoplus_album.py 12345678 \
  --write-caption \
  --gps-lat 31.2304 \
  --gps-lon 121.4737
```

Copy suitable GPS metadata from a reference image:

```bash
python3 scripts/download_photoplus_album.py 12345678 \
  --gps-from-image /path/to/reference.jpg
```

## Options

- Use `--count N` for test runs or partial downloads.
- Use `--tab all` for all photos; date tabs such as `3.28` are matched from photo timestamp metadata by the upstream project.
- Use `--folder-name NAME` to avoid numeric output folders.
- Use `--dry-run` on the wrapper to print the resolved upstream command without network or download work.
- Use `--repo-dir PATH` when an existing clone of `helloene/live-album-downloader` should be reused.
- Use `--install-deps` when `requests`, `tqdm`, or `piexif` are missing.
- Use `--save-metadata` when the user wants JSON sidecars with source item metadata, downloaded file details, and a readable EXIF summary.
- Use `--gps-from-image PATH` to copy latitude, longitude, altitude, speed, speed reference, and horizontal positioning error from a reference image; install `exiftool` first when it is missing.

## Troubleshooting

- If the upstream script prints `Wrong ID`, re-check that the PhotoPlus number came from `/live/<id>` or `/live/pc/<id>`, or that the Pailixiang code came from `/album/a<code>`, and that the album is public/available.
- If dependency installation fails in a sandbox, request approval to run the same `pip`/network command with escalation.
- If `--gps-from-image` fails with a missing `exiftool` message, install the system `exiftool` binary or use explicit `--gps-lat` and `--gps-lon` instead.
- If the album has many photos, first run with `--inspect` or `--count 10`.
- If filenames collide, the upstream project auto-adds suffixes such as `_2`.

## References

- Read `references/upstream-project.md` for the exact upstream repository URL, pinned commit observed while creating this skill, and supported CLI flags.
- Use `scripts/download_photoplus_album.py` as the low-friction command wrapper.
