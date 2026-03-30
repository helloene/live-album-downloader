# Live Album Downloader

PhotoPlus is a photo livestream album service.

Live Album Downloader is a Python tool for downloading [PhotoPlus](https://live.photoplus.cn/) photo livestream albums.
It fetches the album list from the public PhotoPlus endpoint and saves original images to `./PhotoPlus/<activity_id>/`.

中文版请见 [README_CN.md](./README_CN.md)。

## Features

- Download original photos from a PhotoPlus activity
- Filter by tab:
  - `all`
  - date-like tabs such as `3.28` and `3.29`
- Preserve original image files without re-encoding
- Keep file modification time aligned with the photo timestamp
- Prefer the original `pic_name` for downloaded filenames when available
- Optional filename templating
- Preserve the actual downloaded file extension when templating filenames
- Auto-resolve duplicate output filenames with numeric suffixes such as `_2`
- Optional JSON sidecar export with raw metadata
- Optional image metadata writing:
  - IPTC `Caption/Abstract` plus EXIF `UserComment` for the activity title
  - GPS latitude/longitude in EXIF

## Requirements

- Python 3.10+
- `requests`
- `tqdm`

## Get the Project

### Linux / macOS / Windows

#### Git clone

```bash
git clone https://github.com/helloene/live-album-downloader.git
cd live-album-downloader
```

#### Download the script directly

```bash
wget https://raw.githubusercontent.com/helloene/live-album-downloader/main/live_album_downloader.py
```

```bash
curl -L -O https://raw.githubusercontent.com/helloene/live-album-downloader/main/live_album_downloader.py
```

Install the required packages separately:

```bash
pip3 install requests tqdm
```

#### Download the full project ZIP from `main`

```bash
wget https://github.com/helloene/live-album-downloader/archive/refs/heads/main.zip -O live-album-downloader.zip
unzip live-album-downloader.zip
cd live-album-downloader-main
```

#### Download the full project ZIP from `main` with curl

```bash
curl -L https://github.com/helloene/live-album-downloader/archive/refs/heads/main.zip -o live-album-downloader.zip
unzip live-album-downloader.zip
cd live-album-downloader-main
```

#### Download ZIP from GitHub

Open the repository page, choose `Code`, then select `Download ZIP`.

## Dependencies

```bash
pip3 install -r requirements.txt
```

## Usage

Basic download:

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678
```

```powershell
# Windows
python live_album_downloader.py --id 12345678
```

Full featured download:

This example combines multiple optional flags. By default, `--count` is `9999` and `--tab` is `all`, so you only need to pass them when you want different behavior.

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --count 10 --tab 3.29 --folder-name "My Album" --write-caption --gps-lat 0.0000 --gps-lon 0.0000
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --count 10 --tab 3.29 --folder-name "My Album" --write-caption --gps-lat 0.0000 --gps-lon 0.0000
```

Inspect metadata only:

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --inspect
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --inspect
```

Download a specific tab:

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --tab 3.29
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --tab 3.29
```

Custom folder name:

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --folder-name "My Album"
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --folder-name "My Album"
```

Optional filename template:

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --rename-template "{date}_{time}_{name}"
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --rename-template "{date}_{time}_{name}"
```

Optional JSON sidecar:

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --save-metadata
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --save-metadata
```

## Command Line Options

- `--id` Required. PhotoPlus activity ID.
- `--count` Maximum number of photos to fetch. Default: `9999`.
- `--tab` Filter photos by tab. Default: `all`.
- `--rename-template` Optional filename template using `{name}`, `{date}`, `{time}`, `{address}`, and `{tab}`. The actual downloaded file extension is always preserved, and unsupported placeholders fail fast with a clear error. Default: keep the original downloaded filename.
- `--folder-name` Optional output folder name under `PhotoPlus`. Default: activity ID.
- `--no-set-mtime` Disable syncing file modification time from photo timestamp.
- `--save-metadata` Save raw metadata to a JSON sidecar file. Default: disabled.
- `--inspect` Print a metadata summary and tab matching preview.
- `--write-caption` Write the activity title into IPTC `Caption/Abstract` and EXIF `UserComment`. Default: disabled.
- `--gps-lat` GPS latitude.
- `--gps-lon` GPS longitude.
- `--gps-alt` Optional GPS altitude in meters. Default: unset.

## How to Find the Activity ID

Open the PhotoPlus live page and copy the numeric activity ID from the URL.
Both mobile links like `/live/12345678` and PC links like `/live/pc/12345678/#/live` use the same ID.

```text
https://live.photoplus.cn/live/12345678
https://live.photoplus.cn/live/pc/12345678/#/live
```

If you see `Wrong ID`, the usual causes are:

- the ID is not the number in `/live/<id>` or `/live/pc/<id>`
- the ID is `0` or a negative number
- the activity is unavailable, private, expired, or no longer returns data from the PhotoPlus API

## Metadata Behavior

- The script keeps the original image bytes whenever possible.
- By default it keeps the original downloaded filename when possible.
- If two photos resolve to the same output filename, later files are renamed with numeric suffixes such as `_2` to avoid overwriting earlier downloads.
- It does not rewrite image metadata unless `--write-caption` or GPS arguments are provided.
- When enabled, the PhotoPlus page title is written into IPTC `Caption/Abstract` and EXIF `UserComment`.
- Apple Photos/iOS Photos caption support primarily relies on IPTC `Caption/Abstract`.
- GPS metadata is written in standard EXIF GPS format and should use WGS84 coordinates.

## Output

Downloads are saved under:

```text
./PhotoPlus/<activity_id>/
```

If `--folder-name` is provided, the output path becomes:

```text
./PhotoPlus/<folder_name>/
```

## Notes

- Date tabs such as `3.28` and `3.29` are matched from the photo timestamp metadata.
- The script retries transient download failures automatically.
- If a file already exists, the downloader skips the download but can still update optional metadata when enabled.

## Disclaimer

This project is intended for personal archival and lawful use only.
Please make sure you have permission to download and store the photos you access.

## Acknowledgments

This project is based on and modified from [cornultra/photoplus-downloader-python](https://github.com/cornultra/photoplus-downloader-python).

## License

This project is licensed under the MIT License.
