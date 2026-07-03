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
import random
import shutil
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from string import Formatter
from urllib.parse import urlparse, urlencode
from requests.exceptions import RequestException
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

import piexif

SALT = 'laxiaoheiwu'
COUNT = 9999
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30
RENAME_TEMPLATE_FIELDS = {"name", "date", "time", "address", "tab"}
PHOTO_NAME_KEYS = ("pic_name", "picName", "origin_name", "originName", "file_name", "fileName")
PHOTOPLUS_SOURCE = "photoplus"
PAILIXIANG_SOURCE = "pailixiang"
ALLTUU_SOURCE = "alltuu"
OUTPUT_ROOTS = {
    PHOTOPLUS_SOURCE: "PhotoPlus",
    PAILIXIANG_SOURCE: "Pailixiang",
    ALLTUU_SOURCE: "Alltuu",
}
PAILIXIANG_API_BASE = "https://mapi.pailixiang.com/plx"
PAILIXIANG_APP_KEY = "1e3a58fb24de413c9873542fc5667a25"

# Alltuu / Piufoto (m.alltuu.com, www.piufoto.com) live album API.
ALLTUU_API_HOST = "https://v4c.alltuu.com"
ALLTUU_AUTH_HOST = "https://m.alltuu.com"
ALLTUU_CDN_PRIVATE_KEY = "50f403a08b58841d319b92f0c10dbbd2"
ALLTUU_SIGN_FROM = "100002"
ALLTUU_PAGE_SIZE = 60
ALLTUU_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Referer": "https://m.alltuu.com/",
}

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


def _parse_gps_time(value):
    if not value:
        return None
    parts = str(value).strip().split(":")
    if len(parts) < 3:
        return None
    try:
        hour = int(float(parts[0]))
        minute = int(float(parts[1]))
        second = Fraction(float(parts[2])).limit_denominator(1000000)
    except (TypeError, ValueError):
        return None
    return ((hour, 1), (minute, 1), (second.numerator, second.denominator))


def _gps_datetime_from_photo_datetime(dt):
    if not dt:
        return (None, None)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    utc_dt = dt.astimezone(timezone.utc)
    gps_time = (
        (utc_dt.hour, 1),
        (utc_dt.minute, 1),
        (utc_dt.second, 1),
    )
    gps_date = utc_dt.strftime("%Y:%m:%d")
    return gps_time, gps_date


def _build_gps_ifd(lat, lon, alt=None, gps_time=None, gps_date=None, speed=None, speed_ref=None, h_error=None):
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
    parsed_time = gps_time if isinstance(gps_time, tuple) else _parse_gps_time(gps_time)
    if parsed_time:
        gps[piexif.GPSIFD.GPSTimeStamp] = parsed_time
    if gps_date:
        gps[piexif.GPSIFD.GPSDateStamp] = str(gps_date)
    if speed is not None:
        gps[piexif.GPSIFD.GPSSpeedRef] = str(speed_ref or "K")
        gps[piexif.GPSIFD.GPSSpeed] = _rational(float(speed))
    if h_error is not None:
        gps[piexif.GPSIFD.GPSHPositioningError] = _rational(float(h_error))
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


def _parse_album_reference(value, source="auto"):
    text = str(value).strip()
    parsed = urlparse(text)
    host = parsed.hostname or ""

    if source == "auto":
        if host.endswith("alltuu.com") or host.endswith("piufoto.com"):
            source = ALLTUU_SOURCE
        elif host.endswith("pailixiang.com"):
            source = PAILIXIANG_SOURCE
        elif re.fullmatch(r"[0-9a-fA-F]{32}", text):
            source = ALLTUU_SOURCE
        elif re.fullmatch(r"a[A-Za-z0-9_-]+", text):
            source = PAILIXIANG_SOURCE
        else:
            source = PHOTOPLUS_SOURCE

    if source == ALLTUU_SOURCE:
        match = re.search(r"/album/([0-9a-fA-F]{32})", parsed.path or text)
        if match:
            return source, match.group(1).lower()
        match = re.fullmatch(r"[0-9a-fA-F]{32}", text)
        if match:
            return source, text.lower()
        raise SystemExit(
            "Wrong ID: use an Alltuu/Piufoto album URL/code such as a 32-character "
            "album id copied from /album/<id>."
        )

    if source == PAILIXIANG_SOURCE:
        match = re.search(r"/album/a?([A-Za-z0-9_-]+)", parsed.path or text)
        if match:
            return source, match.group(1)
        match = re.fullmatch(r"a?([A-Za-z0-9_-]+)", text)
        if match:
            return source, match.group(1)
        raise SystemExit(
            "Wrong ID: use a Pailixiang album code copied from /album/a<code>."
        )

    match = re.search(r"/live/pc/([1-9]\d*)", text) or re.search(r"/live/([1-9]\d*)", text)
    if match:
        return source, match.group(1)
    if re.fullmatch(r"[1-9]\d*", text):
        return source, text
    raise SystemExit(
        "Wrong ID: use a valid numeric PhotoPlus activity ID copied from /live/<id> "
        "or /live/pc/<id>/ in the URL."
    )


def _pailixiang_ak():
    key = list(PAILIXIANG_APP_KEY)
    prefix = ""
    for _ in range(3):
        number = random.randrange(10)
        prefix += str(number)
        key[number + 15] = key[number]
    return prefix + "".join(key)


def _pailixiang_payload(data, client_type=0, pid="albumview"):
    payload = dict(data)
    payload.update({
        "tt": "",
        "ct": client_type,
        "cv": "151",
        "lang": "cn",
        "pid": pid,
        "ak": _pailixiang_ak(),
    })
    return payload


def _pailixiang_post(path, data, timeout=REQUEST_TIMEOUT):
    response = requests.post(
        f"{PAILIXIANG_API_BASE}{path}",
        json=_pailixiang_payload(data),
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://live.pailixiang.com",
            "Referer": "https://live.pailixiang.com/",
            "Content-Type": "application/json;charset=utf-8",
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("Code") != 0:
        raise SystemExit(f"Pailixiang API error {payload.get('Code')}: {payload.get('Msg')}")
    return payload


def _normalize_pailixiang_photo(item):
    normalized = dict(item)
    normalized["origin_img"] = (
        item.get("DownloadImageUrl")
        or item.get("BigImageUrl")
        or item.get("ImageUrl")
    )
    normalized.setdefault("origin_name", item.get("Name") or item.get("FileName"))
    normalized.setdefault("shoot_time", item.get("ShootTime"))
    normalized.setdefault("photo_time", item.get("ShootTime"))
    normalized.setdefault("activity_name", item.get("AlbumName"))
    return normalized


def _alltuu_cdn_signed_url(path, params):
    """Build an Alltuu CDN-signed URL (md5 of privateKey + sorted path + hex time)."""
    ts = format(int(time.time()), "x")
    filename = path
    for key in sorted(params):
        filename += f"/{key}{params[key]}"
    digest = hashlib.md5((ALLTUU_CDN_PRIVATE_KEY + filename + ts).encode()).hexdigest()
    return f"{ALLTUU_API_HOST}/{digest}/{ts}{filename}"


def _alltuu_server_signed_url(base, path, query):
    """Build an Alltuu server-signed (E8) URL used by the authority endpoint."""
    ts = str(int(time.time() * 1000))
    sign = {"from": ALLTUU_SIGN_FROM, "version": "0", "token": "null", "timestamp": ts}
    sign.update({key: str(value) for key, value in query.items()})
    sign_str = "".join(f"/{sign[key]}" for key in sorted(sign))
    digest = hashlib.md5(sign_str.encode()).hexdigest()
    url = f"{base}{path}/v{ALLTUU_SIGN_FROM}-{ts}-null-0-{digest}"
    if query:
        url += "?" + urlencode(query)
    return url


def _alltuu_get(url, timeout=REQUEST_TIMEOUT):
    response = requests.get(url, timeout=timeout, headers=ALLTUU_HEADERS)
    response.raise_for_status()
    return response.json()


def _alltuu_secret(album_id, timeout=REQUEST_TIMEOUT):
    url = _alltuu_server_signed_url(ALLTUU_AUTH_HOST, "/rest/fc/authority", {"albumId": album_id})
    data = (_alltuu_get(url, timeout=timeout).get("data") or {})
    secret = data.get("secret")
    if not secret:
        raise SystemExit(
            "Wrong ID: use a valid Alltuu/Piufoto album URL/code such as "
            "a 32-character album id copied from /album/<id>."
        )
    return secret


def _alltuu_user_state(album_id, secret, timeout=REQUEST_TIMEOUT):
    url = _alltuu_cdn_signed_url("/rest/v4o/us", {"a": album_id, "sk": secret})
    url += "?t=" + str(5000 * (int(time.time() * 1000) // 5000))
    return _alltuu_get(url, timeout=timeout).get("d") or {}


def _alltuu_album_info(album_id, secret, version, timeout=REQUEST_TIMEOUT):
    url = _alltuu_cdn_signed_url("/rest/v4c/fa", {"a": album_id, "sk": secret, "t": version})
    return _alltuu_get(url, timeout=timeout).get("d") or {}


def _alltuu_fetch_classify_photos(album_id, secret, classify, order, token, limit, timeout=REQUEST_TIMEOUT):
    photos = []
    pc = ""
    while len(photos) < limit:
        params = {
            "a": album_id, "n": ALLTUU_PAGE_SIZE, "o": order, "pc": pc, "pd": "",
            "s": classify, "sk": secret, "t": token, "v": "1",
        }
        page = _alltuu_get(_alltuu_cdn_signed_url("/rest/v4c/fplN", params), timeout=timeout).get("d") or []
        if not page:
            break
        photos.extend(page)
        next_pc = page[-1].get("pc")
        if not next_pc or next_pc == pc:
            break
        pc = next_pc
    return photos[:limit]


def _normalize_alltuu_photo(item, album_title=None, address=None):
    normalized = dict(item)
    normalized["origin_img"] = item.get("ol") or item.get("bl") or item.get("url1920")
    normalized.setdefault("origin_name", item.get("n"))
    normalized.setdefault("photo_time", item.get("time"))
    normalized.setdefault("width", item.get("w"))
    normalized.setdefault("height", item.get("h"))
    if album_title:
        normalized.setdefault("activity_name", album_title)
    if address:
        normalized.setdefault("address", address)
    return normalized


def _encode_utf16le_null(text):
    return str(text).encode("utf-16le") + b"\x00\x00"


def _encode_utf8_bytes(text):
    return str(text).encode("utf-8")


def _first_text(item, keys):
    value = _first_value(item or {}, keys)
    if value is None:
        return ""
    return str(value).strip()


def read_gps_from_reference_image(path):
    if not shutil.which("exiftool"):
        raise SystemExit("exiftool is required for --gps-from-image but was not found in PATH.")
    try:
        output = subprocess.check_output(
            [
                "exiftool", "-j", "-n",
                "-GPSLatitude", "-GPSLongitude", "-GPSAltitude",
                "-GPSSpeed", "-GPSSpeedRef", "-GPSHPositioningError",
                str(path),
            ],
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Failed to read GPS metadata from {path}: {exc}") from exc

    payload = json.loads(output)
    item = payload[0] if payload else {}
    if "GPSLatitude" not in item or "GPSLongitude" not in item:
        raise SystemExit(f"No GPS latitude/longitude found in reference image: {path}")

    gps = {
        "lat": float(item["GPSLatitude"]),
        "lon": float(item["GPSLongitude"]),
    }
    optional_map = {
        "GPSAltitude": "alt",
        "GPSSpeed": "speed",
        "GPSSpeedRef": "speed_ref",
        "GPSHPositioningError": "h_error",
    }
    for src_key, dst_key in optional_map.items():
        if src_key in item and item[src_key] not in (None, ""):
            gps[dst_key] = item[src_key]
    return gps


def _write_optional_image_metadata(image_path, item=None, caption_text=None, gps=None):
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

    item = item or {}
    zeroth = exif_dict.setdefault("0th", {})
    exif = exif_dict.setdefault("Exif", {})

    if caption_text:
        user_comment = b'UNICODE\x00' + caption_text.encode('utf-16-be') + b'\x00\x00'
        exif[piexif.ExifIFD.UserComment] = user_comment
        zeroth[piexif.ImageIFD.ImageDescription] = _encode_utf8_bytes(caption_text)
        zeroth[piexif.ImageIFD.XPTitle] = _encode_utf16le_null(caption_text)
        zeroth[piexif.ImageIFD.XPComment] = _encode_utf16le_null(caption_text)

    photo_name = _first_text(item, ("Name", "name", "origin_name", "FileName", "fileName", "pic_name", "picName"))
    if photo_name:
        zeroth[piexif.ImageIFD.DocumentName] = _encode_utf8_bytes(photo_name)
        zeroth[piexif.ImageIFD.XPSubject] = _encode_utf16le_null(photo_name)

    creator = _first_text(item, ("CreateUserName", "createUserName", "photographer", "author", "user_name", "userName"))
    if creator:
        zeroth[piexif.ImageIFD.Artist] = _encode_utf8_bytes(creator)
        zeroth[piexif.ImageIFD.XPAuthor] = _encode_utf16le_null(creator)

    dt = extract_photo_datetime(item)
    if dt:
        exif_time = dt.strftime("%Y:%m:%d %H:%M:%S")
        zeroth[piexif.ImageIFD.DateTime] = exif_time
        exif[piexif.ExifIFD.DateTimeOriginal] = exif_time
        exif[piexif.ExifIFD.DateTimeDigitized] = exif_time

    width = _first_value(item, ("Width", "width"))
    height = _first_value(item, ("Height", "height"))
    try:
        if width:
            exif[piexif.ExifIFD.PixelXDimension] = int(width)
        if height:
            exif[piexif.ExifIFD.PixelYDimension] = int(height)
    except (TypeError, ValueError):
        pass

    photo_id = _first_text(item, ("ID", "id", "photo_id", "photoId"))
    if photo_id:
        exif[piexif.ExifIFD.ImageUniqueID] = photo_id

    if gps is not None:
        gps_time, gps_date = _gps_datetime_from_photo_datetime(dt)
        exif_dict["GPS"] = _build_gps_ifd(
            gps['lat'], gps['lon'], gps.get('alt'),
            gps_time=gps_time, gps_date=gps_date,
            speed=gps.get('speed'), speed_ref=gps.get('speed_ref'),
            h_error=gps.get('h_error'),
        )

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
    "pailixiang.com",
    "plusx.cn",
    "alltuu.com",
    "piufoto.com",
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

def _json_safe_exif_value(value):
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, tuple):
        return [_json_safe_exif_value(v) for v in value]
    if isinstance(value, list):
        return [_json_safe_exif_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe_exif_value(v) for k, v in value.items()}
    return value


def _decode_exif_text_value(ifd_name, tag_name, value):
    if tag_name in ("XPTitle", "XPComment", "XPAuthor", "XPKeywords", "XPSubject"):
        raw = bytes(value) if isinstance(value, (tuple, list)) else value
        if isinstance(raw, bytes):
            return raw.decode("utf-16le", errors="replace").rstrip("\x00")

    if tag_name == "UserComment" and isinstance(value, bytes):
        if value.startswith(b"UNICODE\x00"):
            return value[8:].decode("utf-16-be", errors="replace").rstrip("\x00")
        if value.startswith(b"ASCII\x00\x00\x00"):
            return value[8:].decode("ascii", errors="replace").rstrip("\x00")

    return _json_safe_exif_value(value)


def read_image_exif_metadata(image_path):
    lower = image_path.lower()
    if not lower.endswith(('.jpg', '.jpeg')):
        return {"present": False, "reason": "not a JPEG file"}

    try:
        exif_dict = piexif.load(image_path)
    except (piexif.InvalidImageDataError, struct.error, ValueError, KeyError) as exc:
        return {"present": False, "reason": str(exc)}

    ifds = {}
    tag_count = 0
    for ifd_name in ("0th", "Exif", "GPS", "Interop", "1st"):
        values = exif_dict.get(ifd_name) or {}
        if not values:
            ifds[ifd_name] = {}
            continue
        readable = {}
        for tag, value in values.items():
            tag_info = piexif.TAGS.get(ifd_name, {}).get(tag, {})
            tag_name = tag_info.get("name", str(tag))
            readable[tag_name] = _decode_exif_text_value(ifd_name, tag_name, value)
        ifds[ifd_name] = readable
        tag_count += len(readable)

    return {
        "present": tag_count > 0,
        "tag_count": tag_count,
        "ifds": ifds,
        "thumbnail_present": bool(exif_dict.get("thumbnail")),
    }


def write_metadata_sidecar(image_path, item):
    sidecar_path = f"{os.path.splitext(image_path)[0]}.json"
    metadata = dict(item)
    metadata["downloaded_file"] = {
        "filename": os.path.basename(image_path),
        "size": os.path.getsize(image_path) if os.path.exists(image_path) else None,
        "mtime": datetime.fromtimestamp(os.path.getmtime(image_path)).isoformat()
        if os.path.exists(image_path) else None,
    }
    metadata["exif"] = read_image_exif_metadata(image_path)
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

def download_image(url, output_dir, item=None, rename_template=None, set_mtime=True, save_metadata=False, tab=None, caption_text=None, gps=None, filename=None):
    if filename is None:
        filename = build_download_name(url, item or {}, rename_template=rename_template, tab=tab)
    image_path = os.path.join(output_dir, filename)
    # Prevent path traversal: ensure resolved path stays inside output_dir
    if not os.path.realpath(image_path).startswith(os.path.realpath(output_dir) + os.sep):
        print(f"Skipping unsafe filename: {filename}")
        return

    def _post_process():
        _write_optional_image_metadata(image_path, item=item, caption_text=caption_text, gps=gps)
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

def fetch_photoplus_activity_result(activity_id, count, timeout=REQUEST_TIMEOUT):
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


def fetch_pailixiang_activity_result(album_code, count, timeout=REQUEST_TIMEOUT):
    public_code = str(album_code).strip().lstrip("a")
    view_payload = _pailixiang_post(
        "/WapAbm/AlbumGetView",
        {
            "ID": public_code,
            "AccessType": "1",
            "SourceType": None,
            "Nw": None,
            "ClientType": 0,
        },
        timeout=timeout,
    )
    view_data = view_payload.get("Data") or {}
    if view_data.get("ResCode", 0) > 0 or not view_data.get("Entity"):
        raise SystemExit(
            "Wrong ID: use a valid Pailixiang album URL/code such as "
            "an album code copied from /album/a<code>."
        )

    entity = view_data["Entity"]
    search_count = min(max(int(view_data.get("PhotoSearchCount") or 100), 1), 100)
    remaining = count
    start_index = 1
    opt_time = ""
    items = []
    total = 0

    while remaining > 0:
        page_size = min(search_count, remaining)
        search_payload = _pailixiang_post(
            "/WapAbm/AlbumSearchPhoto",
            {
                "AlbumID": entity["ID"],
                "GroupID": "",
                "SearchType": 0,
                "IsPayDownload": bool(entity.get("IsPayDownload")),
                "PhotoSortType": entity.get("PhotoSortType", 0),
                "IsNw": bool(view_data.get("IsNw")),
                "IsEmbed": False,
                "StartIndex": start_index,
                "SearchCount": page_size,
                "SortType": entity.get("SortType", 1),
                "OptTime": opt_time,
            },
            timeout=timeout,
        )
        page_items = search_payload.get("Data") or []
        if start_index == 1:
            total = search_payload.get("TotalCount") or len(page_items)
        if not page_items:
            break
        take = page_items[:remaining]
        items.extend(_normalize_pailixiang_photo(item) for item in take)
        start_index += len(page_items)
        remaining -= len(take)
        opt_time = search_payload.get("OptTime") or opt_time
        if remaining <= 0 or len(page_items) < page_size:
            break

    title = entity.get("Title") or entity.get("Name") or f"a{public_code}"
    for item in items:
        if not item.get("activity_name"):
            item["activity_name"] = title

    return {
        "pics_array": items,
        "pics_total": total,
        "activity_name": title,
        "activity_code": entity.get("Code") or f"a{public_code}",
    }


def fetch_alltuu_activity_result(album_id, count, timeout=REQUEST_TIMEOUT):
    secret = _alltuu_secret(album_id, timeout=timeout)
    state = _alltuu_user_state(album_id, secret, timeout=timeout)
    classify_versions = state.get("s") or {}

    album_info = _alltuu_album_info(album_id, secret, state.get("fa"), timeout=timeout)
    album_dto = album_info.get("albumDTO") or {}
    title = album_dto.get("title") or album_id
    address = album_dto.get("adrString")
    default_order = album_dto.get("order", 0)

    classifies = []
    for sep in album_info.get("seperateDTOList") or []:
        classify = str(sep.get("idEnc"))
        version_info = classify_versions.get(classify)
        if not version_info:
            continue
        classifies.append((classify, sep.get("sortType", default_order), version_info.get("v")))
    if not classifies:
        for classify, version_info in classify_versions.items():
            if classify == "0":
                continue
            classifies.append((classify, default_order, version_info.get("v")))

    total = sum(int(classify_versions.get(c, {}).get("t", 0)) for c, _, _ in classifies)

    items = []
    for classify, order, token in classifies:
        remaining = count - len(items)
        if remaining <= 0:
            break
        page = _alltuu_fetch_classify_photos(
            album_id, secret, classify, order, token, remaining, timeout=timeout,
        )
        items.extend(_normalize_alltuu_photo(item, title, address) for item in page)

    return {
        "pics_array": items,
        "pics_total": total or len(items),
        "activity_name": title,
        "activity_code": album_id,
    }


def fetch_activity_result(activity_id, count, source=PHOTOPLUS_SOURCE, timeout=REQUEST_TIMEOUT):
    if source == PAILIXIANG_SOURCE:
        return fetch_pailixiang_activity_result(activity_id, count, timeout=timeout)
    if source == ALLTUU_SOURCE:
        return fetch_alltuu_activity_result(activity_id, count, timeout=timeout)
    return fetch_photoplus_activity_result(activity_id, count, timeout=timeout)


def get_all_images(activity_id, count, tab=None, rename_template=None, set_mtime=True, save_metadata=False, write_caption=False, gps=None, folder_name=None, source=PHOTOPLUS_SOURCE):
    default_folder_name = f"a{activity_id}" if source == PAILIXIANG_SOURCE and not str(activity_id).startswith("a") else activity_id
    folder_name = sanitize_filename(str(folder_name or default_folder_name)).strip() or str(default_folder_name)
    output_root = OUTPUT_ROOTS.get(source, "LiveAlbums")
    output_dir = os.path.join(".", output_root, folder_name)
    result = fetch_activity_result(activity_id, count, source=source)

    print(f"Total photos: {result['pics_total']}, download: {count}")

    os.makedirs(output_dir, exist_ok=True)
    caption_text = None
    if write_caption:
        if source == PHOTOPLUS_SOURCE:
            try:
                caption_text = _read_page_title(activity_id)
            except Exception as exc:
                print(f"Failed to fetch activity title from page: {exc}")
        else:
            caption_text = result.get("activity_name")
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

def inspect_activity(activity_id, count=20, source=PHOTOPLUS_SOURCE):
    result = fetch_activity_result(activity_id, count=min(count, 20), source=source)
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
    parser = argparse.ArgumentParser(description="Download photos from PhotoPlus, Pailixiang or Alltuu/Piufoto")
    parser.add_argument(
        "--id",
        type=str,
        help=(
            "PhotoPlus numeric ID/URL, Pailixiang album code/URL, or "
            "Alltuu/Piufoto album id/URL"
        ),
        required=True,
    )
    parser.add_argument(
        "--source",
        choices=("auto", PHOTOPLUS_SOURCE, PAILIXIANG_SOURCE, ALLTUU_SOURCE),
        default="auto",
        help="Album source; auto detects Pailixiang and Alltuu/Piufoto URLs and otherwise uses PhotoPlus",
    )
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
        help="Optional output folder name under the source output root; defaults to the album ID",
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
    parser.add_argument(
        "--gps-from-image",
        type=str,
        help="Copy suitable GPS metadata from a reference image, excluding direction/bearing angles",
    )

    args = parser.parse_args()

    source, activity_id = _parse_album_reference(args.id, args.source)
    if args.tab and args.tab.lower() == "hot":
        raise SystemExit("Hot tab support has been removed in this version.")

    gps = None
    if args.gps_from_image:
        gps = read_gps_from_reference_image(args.gps_from_image)
    if args.gps_lat is not None or args.gps_lon is not None:
        if args.gps_lat is None or args.gps_lon is None:
            raise SystemExit("Both --gps-lat and --gps-lon are required when writing GPS metadata")
        gps = dict(gps or {})
        gps.update({"lat": args.gps_lat, "lon": args.gps_lon})
    if args.gps_alt is not None:
        if gps is None:
            raise SystemExit("--gps-alt requires --gps-from-image or both --gps-lat and --gps-lon")
        gps = dict(gps)
        gps["alt"] = args.gps_alt

    if args.inspect:
        inspect_activity(activity_id, count=min(args.count, 20), source=source)
    else:
        rename_template = validate_rename_template(args.rename_template)
        get_all_images(
            activity_id, args.count,
            tab=args.tab, rename_template=rename_template,
            set_mtime=not args.no_set_mtime, save_metadata=args.save_metadata,
            write_caption=args.write_caption, gps=gps,
            folder_name=args.folder_name or None,
            source=source,
        )
