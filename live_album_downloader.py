import os
import io
import hashlib
import requests
import argparse
import time
import re
import json
import struct
import html
import stat
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from string import Formatter
from urllib.parse import urlparse
from requests.exceptions import RequestException
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

import piexif

SALT = 'laxiaoheiwu'
COUNT = 9999
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30
OUTPUT_ROOT = "PhotoPlus"
RENAME_TEMPLATE_FIELDS = {"name", "date", "time", "address", "tab"}
PHOTO_NAME_KEYS = ("pic_name", "picName", "origin_name", "originName", "file_name", "fileName")

PHOTOSHOP_APP13_HEADER = b"Photoshop 3.0\x00"
PHOTOSHOP_RESOURCE_SIGNATURE = b"8BIM"
PHOTOSHOP_IPTC_RESOURCE_ID = 0x0404
IPTC_CODED_CHARACTER_SET = (1, 90)
IPTC_CAPTION_ABSTRACT = (2, 120)


def _rational(value):
    """Convert a float to a piexif-compatible (numerator, denominator) tuple."""
    frac = Fraction(value).limit_denominator(1000000)
    return (frac.numerator, frac.denominator)


def _decimal_to_dms(value):
    """Convert decimal degrees to ((d,1),(m,1),(s_num,s_den)) for piexif GPS."""
    value = abs(float(value))
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    sec_frac = Fraction(seconds).limit_denominator(1000000)
    return ((degrees, 1), (minutes, 1), (sec_frac.numerator, sec_frac.denominator))


def _build_gps_ifd(lat, lon, alt=None):
    """Build a piexif GPS IFD dict from decimal WGS84 coordinates."""
    gps = {
        piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: 'N' if lat >= 0 else 'S',
        piexif.GPSIFD.GPSLatitude: _decimal_to_dms(lat),
        piexif.GPSIFD.GPSLongitudeRef: 'E' if lon >= 0 else 'W',
        piexif.GPSIFD.GPSLongitude: _decimal_to_dms(lon),
    }
    if alt is not None:
        gps[piexif.GPSIFD.GPSAltitudeRef] = 1 if alt < 0 else 0
        gps[piexif.GPSIFD.GPSAltitude] = _rational(abs(float(alt)))
    return gps


def _build_iptc_dataset(record_number, dataset_number, data):
    length = len(data)
    if length <= 0x7FFF:
        return bytes([0x1C, record_number, dataset_number]) + struct.pack('>H', length) + data
    return (
        bytes([0x1C, record_number, dataset_number])
        + b'\x80\x04'
        + struct.pack('>I', length)
        + data
    )


def _build_iptc_caption_payload(caption_text):
    """Encode the title as UTF-8 IPTC Caption/Abstract for iOS Photos caption support."""
    datasets = [
        _build_iptc_dataset(*IPTC_CODED_CHARACTER_SET, b'\x1B%G'),
        _build_iptc_dataset(*IPTC_CAPTION_ABSTRACT, caption_text.encode('utf-8')),
    ]
    iptc_payload = b''.join(datasets)
    resource = bytearray()
    resource += PHOTOSHOP_RESOURCE_SIGNATURE
    resource += struct.pack('>H', PHOTOSHOP_IPTC_RESOURCE_ID)
    # Pascal string for resource name: length byte + string + pad to even
    resource_name = b''
    resource += bytes([len(resource_name)]) + resource_name
    # Pad name field (length byte + name bytes) to even
    if (1 + len(resource_name)) % 2 != 0:
        resource += b'\x00'
    resource += struct.pack('>I', len(iptc_payload))
    resource += iptc_payload
    if len(iptc_payload) % 2 != 0:
        resource += b'\x00'
    return PHOTOSHOP_APP13_HEADER + bytes(resource)


def _replace_or_insert_jpeg_segment(jpeg_bytes, new_payload, build_fn, match_fn):
    """Replace or insert a JPEG APP segment.

    build_fn(payload) -> bytes: wraps payload into a full segment (marker + length + payload).
    match_fn(marker, payload_bytes) -> bool: returns True if this segment should be replaced.
    """
    if not jpeg_bytes.startswith(b'\xFF\xD8'):
        return jpeg_bytes

    result = bytearray(jpeg_bytes[:2])
    pos = 2
    inserted = False

    while pos < len(jpeg_bytes):
        if jpeg_bytes[pos] != 0xFF:
            if not inserted:
                result += build_fn(new_payload)
                inserted = True
            result += jpeg_bytes[pos:]
            break

        while pos < len(jpeg_bytes) and jpeg_bytes[pos] == 0xFF:
            pos += 1
        if pos >= len(jpeg_bytes):
            break

        marker = jpeg_bytes[pos]
        pos += 1

        # Standalone markers: SOI, EOI, RST0-RST7 (no length field)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            result += b'\xFF' + bytes([marker])
            continue

        if marker == 0xDA:
            if not inserted:
                result += build_fn(new_payload)
                inserted = True
            result += b'\xFF\xDA' + jpeg_bytes[pos:]
            break

        if pos + 2 > len(jpeg_bytes):
            break
        seg_len = struct.unpack('>H', jpeg_bytes[pos:pos + 2])[0]
        segment = jpeg_bytes[pos - 2:pos + seg_len]
        payload_start = pos + 2
        payload_end = payload_start + seg_len - 2

        if match_fn(marker, jpeg_bytes[payload_start:payload_end]):
            if not inserted:
                result += build_fn(new_payload)
                inserted = True
        else:
            result += segment
        pos += seg_len

    if not inserted:
        result = bytearray(jpeg_bytes[:2]) + build_fn(new_payload) + jpeg_bytes[2:]

    return bytes(result)


def _build_app13_segment(payload):
    length = len(payload) + 2
    return b'\xFF\xED' + struct.pack('>H', length) + payload


def _empty_exif_dict():
    return {
        "0th": {},
        "Exif": {},
        "GPS": {},
        "Interop": {},
        "1st": {},
        "thumbnail": None,
    }


def _normalize_exif_dict(exif_dict):
    normalized = _empty_exif_dict()
    for key, default_value in normalized.items():
        value = exif_dict.get(key, default_value)
        if isinstance(default_value, dict):
            normalized[key] = dict(value or {})
        else:
            normalized[key] = value
    return normalized


def _dump_exif_bytes(exif_dict):
    normalized = _normalize_exif_dict(exif_dict)
    try:
        return piexif.dump(normalized)
    except Exception:
        # Some camera files carry broken EXIF thumbnails; drop them and retry
        # rather than failing the whole metadata update.
        if not normalized.get("1st") and not normalized.get("thumbnail"):
            raise
        normalized["1st"] = {}
        normalized["thumbnail"] = None
        return piexif.dump(normalized)


def _read_page_title(activity_id):
    url = f"https://live.photoplus.cn/live/pc/{activity_id}/"
    response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    html_text = response.text
    match = re.search(r'<title>(.*?)</title>', html_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = html.unescape(match.group(1)).strip()
    if not title:
        return None
    return re.sub(r'\s*[-|·_]\s*PhotoPlus.*$', '', title, flags=re.IGNORECASE)


def _write_optional_image_metadata(image_path, caption_text=None, gps=None):
    """Update JPEG metadata in place while keeping the image bytes otherwise intact."""
    if not caption_text and gps is None:
        return

    lower = image_path.lower()
    if not lower.endswith(('.jpg', '.jpeg')):
        print(f"Skipping metadata write for non-JPEG file: {image_path}")
        return

    original_mode = stat.S_IMODE(os.stat(image_path).st_mode)

    with open(image_path, 'rb') as f:
        jpeg_bytes = f.read()

    # Use piexif for EXIF + GPS metadata
    try:
        exif_dict = _normalize_exif_dict(piexif.load(jpeg_bytes))
    except (piexif.InvalidImageDataError, struct.error, ValueError, KeyError):
        exif_dict = _empty_exif_dict()

    if caption_text:
        user_comment = b'UNICODE\x00' + caption_text.encode('utf-16-be') + b'\x00\x00'
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.UserComment] = user_comment

    if gps is not None:
        exif_dict["GPS"] = _build_gps_ifd(gps['lat'], gps['lon'], gps.get('alt'))

    exif_bytes = _dump_exif_bytes(exif_dict)
    output = io.BytesIO()
    piexif.insert(exif_bytes, jpeg_bytes, output)
    new_jpeg_bytes = output.getvalue()

    # IPTC Caption/Abstract (piexif doesn't handle IPTC, so we keep manual APP13 handling)
    if caption_text:
        iptc_payload = _build_iptc_caption_payload(caption_text)
        new_jpeg_bytes = _replace_or_insert_jpeg_segment(
            new_jpeg_bytes, iptc_payload,
            _build_app13_segment,
            lambda marker, payload: marker == 0xED and payload.startswith(PHOTOSHOP_APP13_HEADER),
        )

    temp_path = f"{image_path}.meta.part"
    with open(temp_path, 'wb') as f:
        f.write(new_jpeg_bytes)
    os.chmod(temp_path, original_mode)
    os.replace(temp_path, image_path)


def obj_key_sort(obj):
    sorted_keys = sorted(obj.keys())
    return '&'.join(f"{key}={obj[key]}" for key in sorted_keys if obj[key] is not None)


def sanitize_filename(filename):
    sanitized = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Prevent path traversal via ".." components
    parts = sanitized.replace('\\', '/').split('/')
    parts = [p for p in parts if p not in ('', '.', '..')]
    return '_'.join(parts) if parts else '_'


def validate_rename_template(rename_template):
    if not rename_template:
        return None

    formatter = Formatter()
    unknown_fields = set()
    try:
        for _, field_name, _, _ in formatter.parse(rename_template):
            if field_name is None:
                continue
            if not field_name:
                raise SystemExit("Invalid --rename-template: empty replacement fields are not supported")
            if field_name not in RENAME_TEMPLATE_FIELDS:
                unknown_fields.add(field_name)
    except ValueError as exc:
        raise SystemExit(f"Invalid --rename-template: {exc}") from exc

    if unknown_fields:
        supported = ", ".join(sorted(RENAME_TEMPLATE_FIELDS))
        invalid = ", ".join(sorted(unknown_fields))
        raise SystemExit(
            f"Invalid --rename-template field(s): {invalid}. Supported fields: {supported}"
        )

    return rename_template


def _source_base_name(item, url):
    for key in PHOTO_NAME_KEYS:
        value = item.get(key)
        if value:
            stem = os.path.splitext(str(value))[0]
            if stem:
                return sanitize_filename(stem)

    fallback = os.path.basename(url.split('#')[0].split('?')[0])
    return sanitize_filename(os.path.splitext(fallback)[0])


def _download_extension(item, url):
    url_name = url.split('#')[0].split('?')[0]
    url_ext = os.path.splitext(url_name)[1]
    if url_ext:
        return url_ext

    for key in PHOTO_NAME_KEYS:
        value = item.get(key)
        if value:
            ext = os.path.splitext(str(value))[1]
            if ext:
                return ext

    return ""


def _preserve_download_extension(filename, original_name):
    _, download_ext = os.path.splitext(original_name)
    if not download_ext:
        return filename
    if filename.lower().endswith(download_ext.lower()):
        return filename
    return f"{filename}{download_ext}"


def _dedupe_download_name(filename, used_names):
    candidate = filename
    root, ext = os.path.splitext(filename)
    counter = 2

    while candidate.lower() in used_names:
        candidate = f"{root}_{counter}{ext}" if root else f"{counter}{ext}"
        counter += 1

    used_names.add(candidate.lower())
    return candidate


ALLOWED_DOWNLOAD_DOMAINS = {
    "photoplus.cn",
    "plusx.cn",
}


def _normalize_download_url(origin_img):
    origin_img = str(origin_img)
    if origin_img.startswith(("http://", "https://")):
        url = origin_img
    elif origin_img.startswith("//"):
        url = f"https:{origin_img}"
    elif origin_img.startswith("/"):
        url = f"https://live.photoplus.cn{origin_img}"
    else:
        url = f"https://{origin_img.lstrip('/')}"
    # Validate domain to avoid downloading from arbitrary hosts
    host = urlparse(url).hostname or ""
    if not any(host == d or host.endswith(f".{d}") for d in ALLOWED_DOWNLOAD_DOMAINS):
        raise ValueError(f"Untrusted download domain: {host}")
    return url


def _first_value(item, keys):
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None

def _parse_timestamp(value):
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        if value > 1_000_000_000_000:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        if value > 100_000_000:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return None

    text = str(value).strip()
    if text.isdigit():
        return _parse_timestamp(int(text))

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    return None

def extract_photo_datetime(item):
    raw = _first_value(item, [
        "exif_timestamp", "exifTimeStamp", "exif_time", "exifTime",
        "photo_time", "photoTime", "shoot_time", "shootTime",
        "create_time", "created_at", "createdAt",
        "upload_time", "uploadTime", "time", "timestamp", "date", "day",
    ])
    return _parse_timestamp(raw)

def extract_address(item):
    return _first_value(item, [
        "address", "location", "shoot_address", "shootAddress",
        "venue", "city", "place",
    ])


def _month_day_variants(month, day):
    return {
        f"{month}.{day}", f"{month:02d}.{day:02d}",
        f"{month}-{day}", f"{month:02d}-{day:02d}",
    }


def _full_date_variants(dt):
    return {
        dt.strftime("%Y-%m-%d"), f"{dt.year}-{dt.month}-{dt.day}",
        dt.strftime("%Y/%m/%d"), f"{dt.year}/{dt.month}/{dt.day}",
        dt.strftime("%Y.%m.%d"), f"{dt.year}.{dt.month}.{dt.day}",
    }


def _datetime_tab_variants(dt):
    return (
        _full_date_variants(dt),
        _month_day_variants(dt.month, dt.day),
        True,  # has_year
    )


def _tab_variants(value):
    if value is None:
        return (set(), set(), False)

    text = str(value).strip().lower()
    if not text:
        return (set(), set(), False)

    normalized = re.sub(r"[\s_]+", "", text)
    simplified = normalized.replace("/", "-").replace(".", "-")

    if re.fullmatch(r"\d{1,4}-\d{1,2}-\d{1,2}", simplified):
        year, month, day = map(int, simplified.split("-"))
        try:
            dt = datetime(year, month, day)
        except ValueError:
            return ({text}, set(), False)
        return _datetime_tab_variants(dt)

    if re.fullmatch(r"\d{1,2}-\d{1,2}", simplified):
        month, day = map(int, simplified.split("-"))
        try:
            datetime(2000, month, day)
        except ValueError:
            return ({text}, set(), False)
        md = _month_day_variants(month, day)
        return (md, md, False)

    return ({text}, set(), False)


def _tab_variants_match(query, item):
    q_exact, q_md, q_has_year = query
    i_exact, i_md, _ = item
    if not q_exact:
        return False
    if q_has_year:
        return bool(q_exact & i_exact)
    if q_md:
        return bool(q_md & i_md)
    return bool(q_exact & i_exact)

def tab_matches(item, tab):
    if not tab or tab.lower() == "all":
        return True

    query_variants = _tab_variants(tab)
    date_value = extract_photo_datetime(item)
    if date_value and _tab_variants_match(query_variants, _datetime_tab_variants(date_value)):
        return True

    for key in ("tab", "tab_name", "tabName", "group", "group_name", "groupName", "date", "day"):
        if key in item and item[key] is not None:
            if _tab_variants_match(query_variants, _tab_variants(item[key])):
                return True

    raw_date = _first_value(item, ("date", "day"))
    parsed_raw_date = _parse_timestamp(raw_date)
    if parsed_raw_date and _tab_variants_match(query_variants, _datetime_tab_variants(parsed_raw_date)):
        return True

    return False

def build_download_name(url, item, rename_template=None, tab=None):
    source_name = _source_base_name(item, url)
    download_ext = _download_extension(item, url)
    original_name = sanitize_filename(f"{source_name}{download_ext}")
    if not rename_template:
        return original_name

    dt = extract_photo_datetime(item)
    address = extract_address(item) or ""
    values = {
        "name": original_name,
        "date": dt.strftime("%Y-%m-%d") if dt else "",
        "time": dt.strftime("%H-%M-%S") if dt else "",
        "address": sanitize_filename(str(address)),
        "tab": sanitize_filename(str(tab or "")),
    }
    filename = sanitize_filename(rename_template.format(**values).strip())
    if not filename:
        return original_name
    return _preserve_download_extension(filename, original_name)

def apply_file_timestamp(path, item):
    dt = extract_photo_datetime(item)
    if not dt:
        return
    ts = dt.timestamp()
    os.utime(path, (ts, ts))

def write_metadata_sidecar(image_path, item):
    sidecar_path = f"{os.path.splitext(image_path)[0]}.json"
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)

def download_image(url, output_dir, item=None, rename_template=None, set_mtime=True, save_metadata=False, tab=None, caption_text=None, gps=None, filename=None):
    if filename is None:
        filename = build_download_name(url, item or {}, rename_template=rename_template, tab=tab)
    image_path = os.path.join(output_dir, filename)
    # Prevent path traversal: ensure resolved path stays inside output_dir
    if not os.path.realpath(image_path).startswith(os.path.realpath(output_dir) + os.sep):
        print(f"Skipping unsafe filename: {filename}")
        return

    def _post_process():
        _write_optional_image_metadata(image_path, caption_text=caption_text, gps=gps)
        if set_mtime and item:
            apply_file_timestamp(image_path, item)
        if save_metadata and item:
            write_metadata_sidecar(image_path, item)

    if os.path.exists(image_path):
        if os.path.getsize(image_path) > 0:
            _post_process()
            return
        os.remove(image_path)

    for attempt in range(1, MAX_RETRIES + 1):
        temp_path = None
        try:
            with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    mode='wb', delete=False, dir=output_dir,
                    # Avoid dot-prefixed temp names: iCloud File Provider can
                    # later propagate the hidden flag to the final renamed file.
                    prefix=f"tmp.{os.path.basename(image_path)}.", suffix=".part",
                ) as file:
                    temp_path = file.name
                    for chunk in response.iter_content(1024 * 64):
                        if chunk:
                            file.write(chunk)

            os.replace(temp_path, image_path)
            temp_path = None
            _post_process()
            return
        except Exception as exc:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)
            else:
                print(f"Failed to download {url}: {exc}")


def _plan_downloads(items, tab=None, rename_template=None):
    planned = []
    used_names = set()
    collision_count = 0

    for item in items:
        origin_img = item.get('origin_img')
        if not origin_img:
            print("Skipping item without origin_img")
            continue

        url = _normalize_download_url(origin_img)
        filename = build_download_name(url, item, rename_template=rename_template, tab=tab)
        unique_filename = _dedupe_download_name(filename, used_names)
        if unique_filename != filename:
            collision_count += 1
        planned.append((item, url, unique_filename))

    if collision_count:
        print(f"Resolved {collision_count} filename collision(s) in this batch")

    return planned

def download_all_images(items, output_dir, tab=None, rename_template=None, set_mtime=True, save_metadata=False, caption_text=None, gps=None):
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        filtered_items = [item for item in items if tab_matches(item, tab)]
        planned_items = _plan_downloads(filtered_items, tab=tab, rename_template=rename_template)

        if tab and tab.lower() != "all":
            print(f"Tab filter: {tab} -> {len(planned_items)} items")

        for item, url, filename in planned_items:
            futures.append(
                executor.submit(
                    download_image, url, output_dir, item, rename_template,
                    set_mtime, save_metadata, tab, caption_text, gps, filename,
                )
            )

        for future in tqdm(as_completed(futures), total=len(futures), desc="Downloading images"):
            try:
                future.result()
            except Exception as exc:
                print(f"Skipping failed download: {exc}")

def fetch_activity_result(activity_id, count, timeout=REQUEST_TIMEOUT):
    t = int(time.time() * 1000)
    data = {
        "activityNo": activity_id,
        "isNew": False,
        "count": count,
        "page": 1,
        "ppSign": "live",
        "picUpIndex": "",
        "_t": t
    }

    data_sort = obj_key_sort(data)
    sign = hashlib.md5((data_sort + SALT).encode()).hexdigest()

    params = {**data, "_s": sign}

    response = requests.get('https://live.photoplus.cn/pic/pics', params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result")
    if not result or "pics_array" not in result:
        raise SystemExit(
            "Wrong ID: use a valid numeric PhotoPlus activity ID copied from /live/<id> or /live/pc/<id>/ in the URL."
        )
    return result

def get_all_images(activity_id, count, tab=None, rename_template=None, set_mtime=True, save_metadata=False, write_caption=False, gps=None, folder_name=None):
    folder_name = sanitize_filename(str(folder_name or activity_id)).strip() or str(activity_id)
    output_dir = os.path.join(".", OUTPUT_ROOT, folder_name)
    result = fetch_activity_result(activity_id, count)

    print(f"Total photos: {result['pics_total']}, download: {count}")

    os.makedirs(output_dir, exist_ok=True)
    caption_text = None
    if write_caption:
        try:
            caption_text = _read_page_title(activity_id)
        except Exception as exc:
            print(f"Failed to fetch activity title from page: {exc}")
        if not caption_text and result.get('pics_array'):
            caption_text = result['pics_array'][0].get('activity_name')
        if caption_text:
            print(f"Caption metadata: {caption_text}")
        else:
            print("Caption metadata: unavailable")

    download_all_images(
        result['pics_array'], output_dir,
        tab=tab, rename_template=rename_template, set_mtime=set_mtime,
        save_metadata=save_metadata, caption_text=caption_text, gps=gps,
    )

def inspect_activity(activity_id, count=20):
    result = fetch_activity_result(activity_id, count=min(count, 20))
    items = result.get('pics_array', [])

    print(f"Total photos: {result.get('pics_total')}, sample items: {len(items)}")
    if not items:
        print("No photos returned.")
        return

    sample = items[0]
    print("Sample keys:")
    print(", ".join(sorted(sample.keys())))

    preview_count = min(count, len(items))
    preview_items = items[:preview_count]
    times = [dt for item in preview_items if (dt := extract_photo_datetime(item)) is not None]
    addresses = [addr for item in preview_items if (addr := extract_address(item))]

    dt = extract_photo_datetime(sample)
    addr = extract_address(sample)
    print(f"Detected time (first item): {dt.isoformat(sep=' ') if dt else 'None'}")
    print(f"Detected address (first item): {addr if addr else 'None'}")

    # Derive tab candidates from actual dates in the sample
    if times:
        tab_candidates = list(dict.fromkeys(f"{t.month}.{t.day}" for t in times))
        matches = {
            candidate: sum(1 for item in preview_items if tab_matches(item, candidate))
            for candidate in tab_candidates[:5]
        }
        print(f"Preview match counts in first {preview_count} items:")
        for candidate, value in matches.items():
            print(f"  {candidate}: {value}")
        print(f"Preview time range: {min(times).isoformat(sep=' ')} -> {max(times).isoformat(sep=' ')}")
    if addresses:
        unique_addresses = list(dict.fromkeys(addresses))
        print(f"Preview addresses: {', '.join(unique_addresses[:5])}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download photos from PhotoPlus")
    parser.add_argument("--id", type=int, help="PhotoPlus ID (e.g., 87654321)", required=True)
    parser.add_argument("--count", type=int, default=COUNT, help="Number of photos to download")
    parser.add_argument(
        "--tab", type=str, default="all",
        help="Download a tab only, e.g. all, 3.29, 3-29, 2026-03-29, 2026-3-29",
    )
    parser.add_argument(
        "--rename-template", type=str, default="",
        help="Optional filename template using {name}, {date}, {time}, {address}, {tab}",
    )
    parser.add_argument(
        "--folder-name", type=str, default="",
        help="Optional output folder name under PhotoPlus; defaults to the activity ID",
    )
    parser.add_argument("--no-set-mtime", action="store_true", help="Do not set file modified time from photo metadata")
    parser.add_argument("--save-metadata", action="store_true", help="Write a JSON sidecar next to each image")
    parser.add_argument("--inspect", action="store_true", help="Inspect the first page of metadata and tab support")
    parser.add_argument(
        "--write-caption", action="store_true",
        help="Write activity title into image caption metadata (IPTC Caption/Abstract + EXIF UserComment)",
    )
    parser.add_argument("--gps-lat", type=float, help="Write GPS latitude into EXIF (WGS84)")
    parser.add_argument("--gps-lon", type=float, help="Write GPS longitude into EXIF (WGS84)")
    parser.add_argument("--gps-alt", type=float, help="Optional GPS altitude in meters")

    args = parser.parse_args()

    if args.id <= 0:
        raise SystemExit("Wrong ID: use a positive numeric PhotoPlus activity ID.")
    if args.tab and args.tab.lower() == "hot":
        raise SystemExit("Hot tab support has been removed in this version.")

    gps = None
    if args.gps_lat is not None or args.gps_lon is not None or args.gps_alt is not None:
        if args.gps_lat is None or args.gps_lon is None:
            raise SystemExit("Both --gps-lat and --gps-lon are required when writing GPS metadata")
        gps = {"lat": args.gps_lat, "lon": args.gps_lon}
        if args.gps_alt is not None:
            gps["alt"] = args.gps_alt

    if args.inspect:
        inspect_activity(args.id, count=min(args.count, 20))
    else:
        rename_template = validate_rename_template(args.rename_template)
        get_all_images(
            args.id, args.count,
            tab=args.tab, rename_template=rename_template,
            set_mtime=not args.no_set_mtime, save_metadata=args.save_metadata,
            write_caption=args.write_caption, gps=gps,
            folder_name=args.folder_name or None,
        )
