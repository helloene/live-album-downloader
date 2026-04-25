# Live Album Downloader

PhotoPlus 是一个图片直播相册服务。

Live Album Downloader 是一个用于下载 [PhotoPlus](https://live.photoplus.cn/) 活动照片的 Python 工具。
它会通过公开接口获取照片列表，并将原图保存到 `./PhotoPlus/<activity_id>/`。

[English version](./README.md)。

## 功能

- 下载 PhotoPlus 活动原图
- 支持按 Tab 过滤：
  - `all`
  - `3.28`、`3.29` 这类日期 Tab
- 保留原始图片，不重新编码
- 将文件系统修改时间对齐到照片拍摄时间
- 下载文件名优先使用原始 `pic_name` 字段
- 支持可选文件名模板
- 使用文件名模板时会保留实际下载文件的原始扩展名
- 输出文件名冲突时会自动追加 `_2` 这类数字后缀避让
- 支持可选 JSON 旁路文件
- 支持可选图片元数据写入：
  - 将活动标题写入 IPTC `Caption/Abstract`，并兼容写入 EXIF `UserComment`
  - 将 GPS 经纬度写入 EXIF

## 依赖

- Python 3.10+
- `requests`
- `tqdm`
- `piexif`

## 获取项目

### 适用于 Linux / macOS / Windows

#### git 克隆

```bash
git clone https://github.com/helloene/live-album-downloader.git
cd live-album-downloader
```

#### 直接下载脚本

```bash
wget https://raw.githubusercontent.com/helloene/live-album-downloader/main/live_album_downloader.py
```

```bash
curl -L -O https://raw.githubusercontent.com/helloene/live-album-downloader/main/live_album_downloader.py
```

依赖包需要单独安装：

```bash
pip3 install requests tqdm piexif
```

#### 从 `main` 分支下载完整项目 ZIP

```bash
wget https://github.com/helloene/live-album-downloader/archive/refs/heads/main.zip -O live-album-downloader.zip
unzip live-album-downloader.zip
cd live-album-downloader-main
```

#### 使用 curl 从 `main` 分支下载完整项目 ZIP

```bash
curl -L https://github.com/helloene/live-album-downloader/archive/refs/heads/main.zip -o live-album-downloader.zip
unzip live-album-downloader.zip
cd live-album-downloader-main
```

#### 从 GitHub 下载压缩包

打开仓库页面，点击 `Code`，再选择 `Download ZIP`。

## 依赖包

```bash
pip3 install -r requirements.txt
```

## Agent Skill

本仓库也包含一个可移植的 Agent Skill：[`skills/photoplus-album-downloader`](./skills/photoplus-album-downloader/)。
它可供 Codex、Claude、OpenClaw/OpenCode，以及其他支持本地 skill 或工具说明的 Agent 使用。

Skill 包包含：

- [`SKILL.md`](./skills/photoplus-album-downloader/SKILL.md)：说明何时使用下载器，以及如何处理 PhotoPlus 图片直播链接或数字活动 ID
- [`scripts/download_photoplus_album.py`](./skills/photoplus-album-downloader/scripts/download_photoplus_album.py)：包装脚本，支持传入 PhotoPlus URL 或活动 ID，可按需准备上游项目并转发支持的参数
- [`references/upstream-project.md`](./skills/photoplus-album-downloader/references/upstream-project.md)：上游 CLI、依赖和可用参数说明

使用时，将 `skills/photoplus-album-downloader` 复制或软链接到对应 Agent 的本地 skills 目录，然后让 Agent 对 PhotoPlus 图片直播链接或活动 ID 使用 `photoplus-album-downloader`。

包装脚本示例：

```bash
python3 skills/photoplus-album-downloader/scripts/download_photoplus_album.py \
  "https://live.photoplus.cn/live/12345678" \
  --workdir ./downloads \
  --install-deps
```

## 用法

基础下载：

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678
```

```powershell
# Windows
python live_album_downloader.py --id 12345678
```

全功能下载示例：

这个示例组合了多个可选参数。默认情况下，`--count` 是 `9999`，`--tab` 是 `all`，只有你想改默认行为时才需要显式传入。

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --count 10 --tab 3.29 --folder-name "My Album" --write-caption --gps-lat 0.0000 --gps-lon 0.0000
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --count 10 --tab 3.29 --folder-name "My Album" --write-caption --gps-lat 0.0000 --gps-lon 0.0000
```

仅查看元数据：

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --inspect
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --inspect
```

下载指定 Tab：

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --tab 3.29
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --tab 3.29
```

自定义输出文件夹名称：

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --folder-name "My Album"
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --folder-name "My Album"
```

可选文件名模板：

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --rename-template "{date}_{time}_{name}"
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --rename-template "{date}_{time}_{name}"
```

可选 JSON 旁路文件：

```bash
# Linux / macOS
python3 live_album_downloader.py --id 12345678 --save-metadata
```

```powershell
# Windows
python live_album_downloader.py --id 12345678 --save-metadata
```

## 命令行参数

- `--id` 必填，PhotoPlus 活动 ID。
- `--count` 最大拉取数量，默认 `9999`。
- `--tab` 按 Tab 过滤，默认 `all`。
- `--rename-template` 可选文件名模板，可使用 `{name}`、`{date}`、`{time}`、`{address}`、`{tab}`。会始终保留实际下载文件的原始扩展名；如果占位符不受支持，会直接给出清晰报错。默认保留下载后的原始文件名。
- `--folder-name` 可选输出文件夹名称，位于 `PhotoPlus` 目录下，默认使用活动 ID。
- `--no-set-mtime` 不再将文件修改时间对齐到拍摄时间。
- `--save-metadata` 保存原始 JSON 元数据，默认关闭。
- `--inspect` 打印元数据摘要和 Tab 匹配结果。
- `--write-caption` 将活动标题写入 IPTC `Caption/Abstract` 和 EXIF `UserComment`，默认关闭。
- `--gps-lat` GPS 纬度。
- `--gps-lon` GPS 经度。
- `--gps-alt` 可选海拔，单位米，默认不写入。

## 如何找到活动 ID

打开 PhotoPlus 活动页面，复制地址栏中的数字活动 ID。
手机链接如 `/live/12345678` 和 PC 链接如 `/live/pc/12345678/#/live` 使用的是同一个 ID。

```text
https://live.photoplus.cn/live/12345678
https://live.photoplus.cn/live/pc/12345678/#/live
```

如果看到 `Wrong ID`，通常是以下原因：

- 填写的不是 `/live/<id>` 或 `/live/pc/<id>` 里的那串数字
- ID 为 `0` 或负数
- 活动已失效、私有、过期，或 PhotoPlus API 已不再返回该活动数据

## 元数据行为

- 默认尽量保留原始图片字节，不做重编码
- 默认尽量保留下载后的原始文件名
- 如果两张照片生成了相同的输出文件名，后面的文件会自动追加 `_2` 这类数字后缀，避免覆盖前面的下载结果
- 只有启用 `--write-caption` 或 GPS 参数时才改写图片元数据
- 启用 `--write-caption` 后，会把 PhotoPlus 页面标题写入 IPTC `Caption/Abstract`，并兼容写入 EXIF `UserComment`
- Apple/iOS 相册的 Caption 主要依赖 IPTC `Caption/Abstract`
- GPS 使用标准 EXIF GPS 格式，建议输入 WGS84 坐标

## 输出目录

下载结果默认保存在：

```text
./PhotoPlus/<activity_id>/
```

如果传入 `--folder-name`，则输出目录变为：

```text
./PhotoPlus/<folder_name>/
```

## 说明

- `3.28`、`3.29` 这类日期 Tab 由照片时间元数据匹配
- 脚本会自动重试临时网络错误
- 如果文件已存在，脚本会跳过下载，但在启用可选元数据功能时仍可补写信息

## 免责声明

本项目仅供个人归档和合法用途使用。
请确保你有权限下载和保存所访问的照片。

## 致谢

本项目基于 [cornultra/photoplus-downloader-python](https://github.com/cornultra/photoplus-downloader-python) 修改而来。

## 许可证

本项目采用 MIT License。
