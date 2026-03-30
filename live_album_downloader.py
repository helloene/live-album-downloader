import os
import hashlib
import requests
import argparse
import time
import re
import json
import struct
import html
import tempfile
from datetime import datetime
from dataclasses import dataclass, field
from fractions import Fraction
from string import Formatter
from requests.exceptions import RequestException
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

SALT = 'laxiaoheiwu'
COUNT = 9999
MAX_RETRIES = 5
REQUEST_TIMEOUT = 30
OUTPUT_ROOT = "PhotoPlus"
RENAME_TEMPLATE_FIELDS = {"name", "date", "time", "address", "tab"}

TIFF_TYPE_SIZES = {
    1: 1,  # BYTE
    2: 1,  # ASCII
    3: 2,  # SHORT
    4: 4,  # LONG
    5: 8,  # RATIONAL
    7: 1,  # UNDEFINED
    9: 4,  # SLONG
    10: 8, # SRATIONAL
}

EXIF_POINTER_TAG = 0x8769
GPS_POINTER_TAG = 0x8825
USER_COMMENT_TAG = 0x9286
GPS_VERSION_TAG = 0x0000
GPS_LATITUDE_REF_TAG = 0x0001
GPS_LATITUDE_TAG = 0x0002
GPS_LONGITUDE_REF_TAG = 0x0003
GPS_LONGITUDE_TAG = 0x0004
GPS_ALTITUDE_REF_TAG = 0x0005
GPS_ALTITUDE_TAG = 0x0006
PHOTOSHOP_APP13_HEADER = b"Photoshop 3.0\x00"
PHOTOSHOP_RESOURCE_SIGNATURE = b"8BIM"
PHOTOSHOP_IPTC_RESOURCE_ID = 0x0404
IPTC_CODED_CHARACTER_SET = (1, 90)
IPTC_CAPTION_ABSTRACT = (2, 120)


@dataclass
class ExifEntry:
    """One TIFF/EXIF entry with raw encoded bytes."""
    tag: int
    type: int
    data: bytes
    count: int | None = None


@dataclass
class IfdNode:
    """A TIFF IFD plus its nested EXIF/GPS child directories."""
    entries: list[ExifEntry] = field(default_factory=list)
    next_ifd: "IfdNode | None" = None
    exif_ifd: "IfdNode | None" = None
    gps_ifd: "IfdNode | None" = None
    offset: int = 0
    total_size: int = 0


def _endian_prefix(endian):
    return '<' if endian == 'II' else '>'


def _u16(data, offset, endian):
    return struct.unpack_from(_endian_prefix(endian) + 'H', data, offset)[0]


def _u32(data, offset, endian):
    return struct.unpack_from(_endian_prefix(endian) + 'I', data, offset)[0]


def _pack_u16(value, endian):
    return struct.pack(_endian_prefix(endian) + 'H', value)


def _pack_u32(value, endian):
    return struct.pack(_endian_prefix(endian) + 'I', value)


def _align_even(value):
    return value if value % 2 == 0 else value + 1


def _make_entry(tag, type_, data, count=None):
    return ExifEntry(tag=tag, type=type_, data=data, count=count)


def _find_entry(node, tag):
    for entry in node.entries:
        if entry.tag == tag:
            return entry
    return None


def _upsert_entry(node, tag, type_, data, count=None):
    entry = _find_entry(node, tag)
    if entry is None:
        node.entries.append(ExifEntry(tag=tag, type=type_, data=data, count=count))
    else:
        entry.type = type_
        entry.data = data
        entry.count = count


def _ascii_bytes(text):
    return text.encode('ascii', errors='ignore') + b'\x00'


def _unicode_user_comment(text):
    # EXIF UserComment supports an 8-byte charset prefix.
    # We keep this as a compatibility layer alongside IPTC Caption/Abstract.
    # UTF-16BE keeps Chinese titles readable in common EXIF readers.
    return b'UNICODE\x00' + text.encode('utf-16-be') + b'\x00\x00'


def _rational_bytes(value, endian):
    frac = Fraction(value).limit_denominator(1000000)
    return _pack_u32(frac.numerator, endian) + _pack_u32(frac.denominator, endian)


def _decimal_to_dms(value, ref_positive, ref_negative, endian):
    ref = ref_positive if value >= 0 else ref_negative
    value = abs(float(value))
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = (minutes_float - minutes) * 60
    return ref, [
        _rational_bytes(degrees, endian),
        _rational_bytes(minutes, endian),
        _rational_bytes(seconds, endian),
    ]


def _parse_tiff_ifd(tiff, ifd_offset, endian, cache):
    # IFDs can reference each other recursively, so cache by offset.
    if ifd_offset in cache:
        return cache[ifd_offset]
    if ifd_offset <= 0 or ifd_offset + 2 > len(tiff):
        return None

    entry_count = _u16(tiff, ifd_offset, endian)
    cursor = ifd_offset + 2
    node = IfdNode()
    node._endian = endian
    cache[ifd_offset] = node

    for _ in range(entry_count):
        if cursor + 12 > len(tiff):
            break
        tag = _u16(tiff, cursor, endian)
        type_ = _u16(tiff, cursor + 2, endian)
        count = _u32(tiff, cursor + 4, endian)
        raw = tiff[cursor + 8:cursor + 12]
        unit = TIFF_TYPE_SIZES.get(type_, 1)
        data_len = unit * count
        if data_len <= 4:
            data = raw[:data_len]
        else:
            value_offset = _u32(raw, 0, endian)
            if value_offset < len(tiff):
                data = tiff[value_offset:value_offset + data_len]
            else:
                data = b''
        entry = ExifEntry(tag=tag, type=type_, data=data, count=count)
        node.entries.append(entry)
        cursor += 12

    next_ifd_offset = _u32(tiff, cursor, endian) if cursor + 4 <= len(tiff) else 0

    # Resolve EXIF/GPS pointer tags into child nodes so callers can mutate
    # metadata without tracking raw offsets by hand.
    for entry in node.entries:
        if entry.tag in (EXIF_POINTER_TAG, GPS_POINTER_TAG):
            pointer_value = _u32(entry.data.ljust(4, b'\x00'), 0, endian) if entry.data else 0
            child = _parse_tiff_ifd(tiff, pointer_value, endian, cache)
            if entry.tag == EXIF_POINTER_TAG:
                node.exif_ifd = child
            else:
                node.gps_ifd = child

    node.next_ifd = _parse_tiff_ifd(tiff, next_ifd_offset, endian, cache)
    return node


def _parse_exif_payload(exif_payload):
    """Parse an APP1 EXIF payload into a TIFF tree we can update and rebuild."""
    if not exif_payload or not exif_payload.startswith(b'Exif\x00\x00'):
        return None, None

    tiff = exif_payload[6:]
    if len(tiff) < 8:
        return None, None

    endian = tiff[:2].decode('ascii', errors='ignore')
    if endian not in ('II', 'MM'):
        return None, None

    magic = _u16(tiff, 2, endian)
    if magic != 42:
        return None, None

    root_offset = _u32(tiff, 4, endian)
    cache = {}
    root = _parse_tiff_ifd(tiff, root_offset, endian, cache)
    return root, endian


def _layout_ifd(node, start_offset, endian):
    # TIFF stores variable-size payloads out-of-line, so we first assign stable
    # offsets to each directory before serializing the actual bytes.
    if node is None:
        return start_offset

    node.offset = start_offset
    node._endian = endian
    entries = sorted(node.entries, key=lambda entry: entry.tag)
    fixed_size = 2 + len(entries) * 12 + 4
    extra_cursor = start_offset + fixed_size

    for entry in entries:
        value_len = len(entry.data)
        if value_len > 4:
            extra_cursor = _align_even(extra_cursor)
            extra_cursor += value_len

    node.total_size = extra_cursor - start_offset
    child_cursor = start_offset + node.total_size
    child_cursor = _layout_ifd(node.exif_ifd, child_cursor, endian)
    child_cursor = _layout_ifd(node.gps_ifd, child_cursor, endian)
    child_cursor = _layout_ifd(node.next_ifd, child_cursor, endian)
    return child_cursor


def _serialize_ifd(node, endian):
    # Serialize one IFD using the offsets computed by _layout_ifd().
    entries = sorted(node.entries, key=lambda entry: entry.tag)
    fixed_size = 2 + len(entries) * 12 + 4
    extra_cursor = node.offset + fixed_size
    extra_blocks = []
    entry_blobs = []

    for entry in entries:
        data = entry.data
        count = entry.count
        if entry.tag == EXIF_POINTER_TAG and node.exif_ifd is not None:
            data = _pack_u32(node.exif_ifd.offset, endian)
            count = 1
        elif entry.tag == GPS_POINTER_TAG and node.gps_ifd is not None:
            data = _pack_u32(node.gps_ifd.offset, endian)
            count = 1
        elif count is None:
            unit = TIFF_TYPE_SIZES.get(entry.type, 1)
            count = max(1, len(data) // unit if unit else len(data))

        if len(data) <= 4:
            value_field = data.ljust(4, b'\x00')
        else:
            extra_cursor = _align_even(extra_cursor)
            value_field = _pack_u32(extra_cursor, endian)
            extra_blocks.append((extra_cursor, data))
            extra_cursor += len(data)

        entry_blobs.append(
            _pack_u16(entry.tag, endian)
            + _pack_u16(entry.type, endian)
            + _pack_u32(count, endian)
            + value_field
        )

    blob = bytearray()
    blob += _pack_u16(len(entries), endian)
    for entry_blob in entry_blobs:
        blob += entry_blob
    next_ifd_offset = node.next_ifd.offset if node.next_ifd is not None else 0
    blob += _pack_u32(next_ifd_offset, endian)

    expected_size = node.total_size
    if len(blob) < expected_size:
        blob += b'\x00' * (expected_size - len(blob))

    for offset, data in extra_blocks:
        start = offset - node.offset
        blob[start:start + len(data)] = data

    return bytes(blob)


def _build_exif_payload(root, endian):
    if root is None:
        root = IfdNode()
        root._endian = endian

    _layout_ifd(root, 8, endian)
    total_size = root.offset + root.total_size

    # Include child IFD payloads in the final size so nested EXIF/GPS data is
    # preserved when we rebuild the APP1 segment.
    stack = [root]
    max_end = total_size
    while stack:
        node = stack.pop()
        for child in (node.exif_ifd, node.gps_ifd, node.next_ifd):
            if child is not None:
                max_end = max(max_end, child.offset + child.total_size)
                stack.append(child)

    tiff = bytearray(max_end)
    tiff[0:2] = endian.encode('ascii')
    tiff[2:4] = _pack_u16(42, endian)
    tiff[4:8] = _pack_u32(root.offset, endian)

    stack = [root]
    seen = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        node_bytes = _serialize_ifd(node, endian)
        tiff[node.offset:node.offset + len(node_bytes)] = node_bytes
        for child in (node.exif_ifd, node.gps_ifd, node.next_ifd):
            if child is not None:
                stack.append(child)

    return b'Exif\x00\x00' + bytes(tiff)


def _build_app1_segment(payload):
    length = len(payload) + 2
    return b'\xFF\xE1' + struct.pack('>H', length) + payload


def _build_app13_segment(payload):
    length = len(payload) + 2
    return b'\xFF\xED' + struct.pack('>H', length) + payload


def _replace_or_insert_exif_segment(jpeg_bytes, new_exif_payload):
    """Replace the first EXIF APP1 segment or insert a new one before image data."""
    if not jpeg_bytes.startswith(b'\xFF\xD8'):
        return jpeg_bytes

    result = bytearray()
    result += jpeg_bytes[:2]
    pos = 2
    inserted = False

    while pos < len(jpeg_bytes):
        if jpeg_bytes[pos] != 0xFF:
            if not inserted:
                result += _build_app1_segment(new_exif_payload)
                inserted = True
            result += jpeg_bytes[pos:]
            break

        while pos < len(jpeg_bytes) and jpeg_bytes[pos] == 0xFF:
            pos += 1
        if pos >= len(jpeg_bytes):
            break

        marker = jpeg_bytes[pos]
        pos += 1

        if marker in (0xD8, 0xD9):
            result += b'\xFF' + bytes([marker])
            continue

        if marker == 0xDA:  # Start of Scan
            if not inserted:
                result += _build_app1_segment(new_exif_payload)
                inserted = True
            result += b'\xFF\xDA' + jpeg_bytes[pos:]
            break

        if pos + 2 > len(jpeg_bytes):
            break
        seg_len = struct.unpack('>H', jpeg_bytes[pos:pos + 2])[0]
        segment = jpeg_bytes[pos - 2:pos + seg_len]
        is_exif = marker == 0xE1 and jpeg_bytes[pos + 2:pos + 8] == b'Exif\x00\x00'
        if is_exif:
            if not inserted:
                result += _build_app1_segment(new_exif_payload)
                inserted = True
        else:
            result += segment
        pos += seg_len

    if not inserted:
        result = bytearray(jpeg_bytes[:2]) + _build_app1_segment(new_exif_payload) + jpeg_bytes[2:]

    return bytes(result)


def _build_iptc_dataset(record_number, dataset_number, data):
    """Build one IPTC IIM dataset for the APP13 Photoshop resource block."""
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
    resource += b'\x00'
    if len(resource) % 2 != 0:
        resource += b'\x00'
    resource += struct.pack('>I', len(iptc_payload))
    resource += iptc_payload
    if len(iptc_payload) % 2 != 0:
        resource += b'\x00'
    return PHOTOSHOP_APP13_HEADER + bytes(resource)


def _replace_or_insert_photoshop_app13_segment(jpeg_bytes, app13_payload):
    """Replace Photoshop APP13 metadata or insert it before JPEG image data."""
    if not jpeg_bytes.startswith(b'\xFF\xD8'):
        return jpeg_bytes

    result = bytearray()
    result += jpeg_bytes[:2]
    pos = 2
    inserted = False

    while pos < len(jpeg_bytes):
        if jpeg_bytes[pos] != 0xFF:
            if not inserted:
                result += _build_app13_segment(app13_payload)
                inserted = True
            result += jpeg_bytes[pos:]
            break

        while pos < len(jpeg_bytes) and jpeg_bytes[pos] == 0xFF:
            pos += 1
        if pos >= len(jpeg_bytes):
            break

        marker = jpeg_bytes[pos]
        pos += 1

        if marker in (0xD8, 0xD9):
            result += b'\xFF' + bytes([marker])
            continue

        if marker == 0xDA:
            if not inserted:
                result += _build_app13_segment(app13_payload)
                inserted = True
            result += b'\xFF\xDA' + jpeg_bytes[pos:]
            break

        if pos + 2 > len(jpeg_bytes):
            break
        seg_len = struct.unpack('>H', jpeg_bytes[pos:pos + 2])[0]
        segment = jpeg_bytes[pos - 2:pos + seg_len]
        payload_start = pos + 2
        payload_end = payload_start + seg_len - 2
        is_photoshop_app13 = (
            marker == 0xED
            and jpeg_bytes[payload_start:payload_end].startswith(PHOTOSHOP_APP13_HEADER)
        )
        if is_photoshop_app13:
            if not inserted:
                result += _build_app13_segment(app13_payload)
                inserted = True
        else:
            result += segment
        pos += seg_len

    if not inserted:
        result = bytearray(jpeg_bytes[:2]) + _build_app13_segment(app13_payload) + jpeg_bytes[2:]

    return bytes(result)


def _set_file_bytes_atomic(path, content):
    temp_path = f"{path}.meta.part"
    with open(temp_path, 'wb') as f:
        f.write(content)
    os.replace(temp_path, path)


def _gps_entry(tag, type_, data, count):
    return _make_entry(tag, type_, data, count)


def _build_gps_node(lat, lon, alt=None, endian='II'):
    """Build a minimal GPS IFD from decimal WGS84 coordinates."""
    node = IfdNode()
    node._endian = endian

    lat_ref, lat_parts = _decimal_to_dms(lat, 'N', 'S', endian)
    lon_ref, lon_parts = _decimal_to_dms(lon, 'E', 'W', endian)

    node.entries.extend([
        _gps_entry(GPS_VERSION_TAG, 1, bytes([2, 3, 0, 0]), 4),
        _gps_entry(GPS_LATITUDE_REF_TAG, 2, _ascii_bytes(lat_ref), 2),
        _gps_entry(GPS_LATITUDE_TAG, 5, b''.join(lat_parts), 3),
        _gps_entry(GPS_LONGITUDE_REF_TAG, 2, _ascii_bytes(lon_ref), 2),
        _gps_entry(GPS_LONGITUDE_TAG, 5, b''.join(lon_parts), 3),
    ])

    if alt is not None:
        alt_ref = 1 if alt < 0 else 0
        alt_value = abs(float(alt))
        node.entries.extend([
            _gps_entry(GPS_ALTITUDE_REF_TAG, 1, bytes([alt_ref]), 1),
            _gps_entry(GPS_ALTITUDE_TAG, 5, _rational_bytes(alt_value, endian), 1),
        ])

    return node


def _ensure_metadata_tree(root, caption_text=None, gps=None, endian='II'):
    """Create missing EXIF/GPS directories before writing new metadata."""
    if root is None:
        root = IfdNode()
        root._endian = endian

    if caption_text:
        if root.exif_ifd is None:
            root.exif_ifd = IfdNode()
            root.exif_ifd._endian = endian
        comment_data = _unicode_user_comment(caption_text)
        _upsert_entry(root.exif_ifd, USER_COMMENT_TAG, 7, comment_data, len(comment_data))
        if not any(entry.tag == EXIF_POINTER_TAG for entry in root.entries):
            root.entries.append(_make_entry(EXIF_POINTER_TAG, 4, _pack_u32(0, endian), 1))

    if gps is not None:
        if root.gps_ifd is None:
            root.gps_ifd = _build_gps_node(gps['lat'], gps['lon'], gps.get('alt'), endian=endian)
        else:
            root.gps_ifd = _build_gps_node(gps['lat'], gps['lon'], gps.get('alt'), endian=endian)
        if not any(entry.tag == GPS_POINTER_TAG for entry in root.entries):
            root.entries.append(_make_entry(GPS_POINTER_TAG, 4, _pack_u32(0, endian), 1))

    return root


def _read_page_title(activity_id):
    """Read the PhotoPlus page title so it can be reused as caption metadata."""
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
    lower = image_path.lower()
    if not lower.endswith(('.jpg', '.jpeg')):
        if caption_text or gps:
            print(f"Skipping metadata write for non-JPEG file: {image_path}")
        return

    with open(image_path, 'rb') as f:
        jpeg_bytes = f.read()

    root, endian = _parse_exif_payload(_extract_exif_payload(jpeg_bytes))
    if root is None:
        endian = 'II'

    root = _ensure_metadata_tree(root, caption_text=caption_text, gps=gps, endian=endian)
    new_exif_payload = _build_exif_payload(root, endian)
    new_jpeg_bytes = _replace_or_insert_exif_segment(jpeg_bytes, new_exif_payload)
    if caption_text:
        iptc_payload = _build_iptc_caption_payload(caption_text)
        new_jpeg_bytes = _replace_or_insert_photoshop_app13_segment(new_jpeg_bytes, iptc_payload)
    _set_file_bytes_atomic(image_path, new_jpeg_bytes)


def _extract_exif_payload(jpeg_bytes):
    """Return the raw EXIF APP1 payload from a JPEG file, if present."""
    if not jpeg_bytes.startswith(b'\xFF\xD8'):
        return None

    pos = 2
    while pos + 4 <= len(jpeg_bytes):
        if jpeg_bytes[pos] != 0xFF:
            break
        while pos < len(jpeg_bytes) and jpeg_bytes[pos] == 0xFF:
            pos += 1
        if pos >= len(jpeg_bytes):
            break
        marker = jpeg_bytes[pos]
        pos += 1
        if marker in (0xD8, 0xD9):
            continue
        if marker == 0xDA:
            break
        if pos + 2 > len(jpeg_bytes):
            break
        seg_len = struct.unpack('>H', jpeg_bytes[pos:pos + 2])[0]
        payload_start = pos + 2
        payload_end = payload_start + seg_len - 2
        if marker == 0xE1 and jpeg_bytes[payload_start:payload_start + 6] == b'Exif\x00\x00':
            return jpeg_bytes[payload_start:payload_end]
        pos = payload_end

    return None

def obj_key_sort(obj):
    """Build the sorted query-string format expected by the PhotoPlus signer."""
    sorted_keys = sorted(obj.keys())
    new_obj = []
    for key in sorted_keys:
        if obj[key] is not None:
            value = str(obj[key])
            new_obj.append(f"{key}={value}")
    return '&'.join(new_obj)

def sanitize_filename(filename):
    """Remove characters that are invalid on common desktop filesystems."""
    return re.sub(r'[<>:"/\\|?*]', '_', filename)


def validate_rename_template(rename_template):
    """Reject unsupported placeholders early so batch runs fail clearly."""
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
    """Prefer the platform's original photo name when it exists."""
    for key in ("pic_name", "picName", "origin_name", "originName", "file_name", "fileName"):
        value = item.get(key)
        if value:
            stem = os.path.splitext(str(value))[0]
            if stem:
                return sanitize_filename(stem)

    fallback = os.path.basename(url.split('#')[0].split('?')[0])
    return sanitize_filename(os.path.splitext(fallback)[0])


def _download_extension(item, url):
    """Keep the extension from the delivered file URL when possible."""
    url_name = url.split('#')[0].split('?')[0]
    url_ext = os.path.splitext(url_name)[1]
    if url_ext:
        return url_ext

    for key in ("pic_name", "picName", "origin_name", "originName", "file_name", "fileName"):
        value = item.get(key)
        if value:
            ext = os.path.splitext(str(value))[1]
            if ext:
                return ext

    return ""


def _preserve_download_extension(filename, original_name):
    """Keep the actual downloaded file extension even when templates contain dots."""
    _, download_ext = os.path.splitext(original_name)
    if not download_ext:
        return filename
    if filename.lower().endswith(download_ext.lower()):
        return filename
    return f"{filename}{download_ext}"


def _dedupe_download_name(filename, used_names):
    """Avoid same-batch filename collisions on case-insensitive filesystems."""
    candidate = filename
    root, ext = os.path.splitext(filename)
    counter = 2

    while candidate.lower() in used_names:
        candidate = f"{root}_{counter}{ext}" if root else f"{counter}{ext}"
        counter += 1

    used_names.add(candidate.lower())
    return candidate


def _normalize_download_url(origin_img):
    """Accept scheme-less, absolute, and relative PhotoPlus image URLs."""
    origin_img = str(origin_img)
    if origin_img.startswith(("http://", "https://")):
        return origin_img
    if origin_img.startswith("//"):
        return f"https:{origin_img}"
    if origin_img.startswith("/"):
        return f"https://live.photoplus.cn{origin_img}"
    return f"https://{origin_img.lstrip('/')}"


def _first_value(item, keys):
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None

def _parse_timestamp(value):
    """Parse timestamps from the different shapes PhotoPlus may return."""
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        if value > 1_000_000_000_000:
            return datetime.fromtimestamp(value / 1000)
        if value > 1_000_000_000:
            return datetime.fromtimestamp(value)
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
    """Best-effort timestamp extraction from PhotoPlus photo metadata."""
    raw = _first_value(item, [
        "exif_timestamp",
        "exifTimeStamp",
        "exif_time",
        "exifTime",
        "photo_time",
        "photoTime",
        "shoot_time",
        "shootTime",
        "create_time",
        "created_at",
        "createdAt",
        "upload_time",
        "uploadTime",
        "time",
        "timestamp",
        "date",
        "day",
    ])
    return _parse_timestamp(raw)

def extract_address(item):
    return _first_value(item, [
        "address",
        "location",
        "shoot_address",
        "shootAddress",
        "venue",
        "city",
        "place",
    ])

def tab_matches(item, tab):
    """Match PhotoPlus date-like tabs against the metadata we have locally."""
    if not tab or tab.lower() == "all":
        return True

    tab_lower = tab.lower()
    # Date tabs such as 3.28 / 3.29 usually represent photos from a day group.
    date_value = extract_photo_datetime(item)
    if date_value:
        if tab_lower in (
            f"{date_value.month}.{date_value.day}",
            f"{date_value.month:02d}.{date_value.day:02d}",
            date_value.strftime("%Y-%m-%d"),
        ):
            return True

    for key in ("tab", "tab_name", "tabName", "group", "group_name", "groupName", "date", "day"):
        if key in item and item[key] is not None and tab_lower == str(item[key]).lower():
            return True

    return False

def build_download_name(url, item, rename_template=None, tab=None):
    """Build the output filename, optionally using metadata placeholders."""
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
    filename = rename_template.format(**values).strip()
    filename = sanitize_filename(filename)
    if not filename:
        return original_name
    return _preserve_download_extension(filename, original_name)

def apply_file_timestamp(path, item):
    """Align filesystem mtime with the detected photo timestamp when available."""
    dt = extract_photo_datetime(item)
    if not dt:
        return
    ts = dt.timestamp()
    os.utime(path, (ts, ts))

def write_metadata_sidecar(image_path, item):
    """Persist the raw API payload next to the image for debugging or reuse."""
    sidecar_path = f"{os.path.splitext(image_path)[0]}.json"
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)

def download_image(url, output_dir, item=None, rename_template=None, set_mtime=True, save_metadata=False, tab=None, caption_text=None, gps=None, filename=None):
    """Download one image and then apply optional file-system or image metadata."""
    if filename is None:
        filename = build_download_name(url, item or {}, rename_template=rename_template, tab=tab)
    image_path = os.path.join(output_dir, filename)
    temp_path = None

    if os.path.exists(image_path):
        if caption_text or gps:
            _write_optional_image_metadata(image_path, caption_text=caption_text, gps=gps)
        if set_mtime and item:
            apply_file_timestamp(image_path, item)
        if save_metadata and item:
            write_metadata_sidecar(image_path, item)
        return

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        temp_path = None
        try:
            with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
                response.raise_for_status()

                # Write to a unique temporary file first so parallel downloads
                # never share the same .part path.
                with tempfile.NamedTemporaryFile(
                    mode='wb',
                    delete=False,
                    dir=output_dir,
                    prefix=f".{os.path.basename(image_path)}.",
                    suffix=".part",
                ) as file:
                    temp_path = file.name
                    for chunk in response.iter_content(1024 * 64):
                        if chunk:
                            file.write(chunk)

            os.replace(temp_path, image_path)
            temp_path = None
            if caption_text or gps:
                _write_optional_image_metadata(image_path, caption_text=caption_text, gps=gps)
            if set_mtime and item:
                apply_file_timestamp(image_path, item)
            if save_metadata and item:
                write_metadata_sidecar(image_path, item)
            return
        except RequestException as exc:
            last_error = exc
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)
            else:
                print(f"Failed to download {url}: {exc}")
        except Exception as exc:
            last_error = exc
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)
            else:
                print(f"Failed to download {url}: {exc}")

    if last_error:
        return


def _plan_downloads(items, tab=None, rename_template=None):
    """Precompute stable filenames so duplicates do not race each other."""
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
    """Filter the activity payload and fan out downloads across a thread pool."""
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        filtered_items = [item for item in items if tab_matches(item, tab)]
        planned_items = _plan_downloads(filtered_items, tab=tab, rename_template=rename_template)

        if tab and tab.lower() != "all":
            print(f"Tab filter: {tab} -> {len(planned_items)} items")

        for item, url, filename in planned_items:
            futures.append(
                executor.submit(
                    download_image,
                    url,
                    output_dir,
                    item,
                    rename_template,
                    set_mtime,
                    save_metadata,
                    tab,
                    caption_text,
                    gps,
                    filename,
                )
            )
        
        for future in tqdm(as_completed(futures), total=len(futures), desc="Downloading images"):
            try:
                future.result()
            except Exception as exc:
                print(f"Skipping failed download: {exc}")

def fetch_activity_result(activity_id, count, timeout=REQUEST_TIMEOUT):
    """Fetch one PhotoPlus activity payload and normalize invalid-ID errors."""
    t = int(time.time() * 1000)  # Current timestamp in milliseconds
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
    
    params = {
        **data,
        "_s": sign,
        "ppSign": "live",
        "picUpIndex": "",
    }

    # The endpoint expects a salted MD5 signature built from the sorted params.
    response = requests.get('https://live.photoplus.cn/pic/pics', params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result")
    if not result or "pics_array" not in result:
        raise SystemExit(
            "Wrong ID: use a valid numeric PhotoPlus activity ID copied from /live/<id> or /live/pc/<id>/ in the URL."
        )
    return result

def get_all_images(id, count, tab=None, rename_template=None, set_mtime=True, save_metadata=False, write_caption=False, gps=None, folder_name=None):
    """Download one activity and apply the requested post-processing options."""
    folder_name = sanitize_filename(str(folder_name or id)).strip() or str(id)
    output_dir = os.path.join(".", OUTPUT_ROOT, folder_name)
    result = fetch_activity_result(id, count)

    print(f"Total photos: {result['pics_total']}, download: {count}")

    os.makedirs(output_dir, exist_ok=True)
    caption_text = None
    if write_caption:
        try:
            caption_text = _read_page_title(id)
        except Exception as exc:
            print(f"Failed to fetch activity title from page: {exc}")
        if not caption_text and result.get('pics_array'):
            caption_text = result['pics_array'][0].get('activity_name')
        if caption_text:
            print(f"Caption metadata: {caption_text}")
        else:
            print("Caption metadata: unavailable")

    download_all_images(
        result['pics_array'],
        output_dir,
        tab=tab,
        rename_template=rename_template,
        set_mtime=set_mtime,
        save_metadata=save_metadata,
        caption_text=caption_text,
        gps=gps,
    )

def inspect_activity(id, count=20):
    """Print a compact metadata preview to help validate a given activity."""
    result = fetch_activity_result(id, count=min(count, 20))
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
    matches = {
        candidate: sum(1 for item in preview_items if tab_matches(item, candidate))
        for candidate in ("3.28", "3.29")
    }
    times = [extract_photo_datetime(item) for item in preview_items]
    times = [dt for dt in times if dt is not None]
    addresses = [extract_address(item) for item in preview_items if extract_address(item)]

    dt = extract_photo_datetime(sample)
    addr = extract_address(sample)
    print(f"Detected time (first item): {dt.isoformat(sep=' ') if dt else 'None'}")
    print(f"Detected address (first item): {addr if addr else 'None'}")
    print(f"Preview match counts in first {preview_count} items:")
    for candidate, value in matches.items():
        print(f"  {candidate}: {value}")
    if times:
        print(f"Preview time range: {min(times).isoformat(sep=' ')} -> {max(times).isoformat(sep=' ')}")
    if addresses:
        unique_addresses = list(dict.fromkeys(addresses))
        print(f"Preview addresses: {', '.join(unique_addresses[:5])}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download photos from PhotoPlus")
    parser.add_argument("--id", type=int, help="PhotoPlus ID (e.g., 87654321)", required=True)
    parser.add_argument("--count", type=int, default=COUNT, help="Number of photos to download")
    parser.add_argument("--tab", type=str, default="all", help="Download a tab only, e.g. all, 3.28, 3.29")
    parser.add_argument(
        "--rename-template",
        type=str,
        default="",
        help="Optional filename template using {name}, {date}, {time}, {address}, {tab}",
    )
    parser.add_argument(
        "--folder-name",
        type=str,
        default="",
        help="Optional output folder name under PhotoPlus; defaults to the activity ID",
    )
    parser.add_argument("--no-set-mtime", action="store_true", help="Do not set file modified time from photo metadata")
    parser.add_argument("--save-metadata", action="store_true", help="Write a JSON sidecar next to each image")
    parser.add_argument("--inspect", action="store_true", help="Inspect the first page of metadata and tab support")
    parser.add_argument(
        "--write-caption",
        action="store_true",
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
            args.id,
            args.count,
            tab=args.tab,
            rename_template=rename_template,
            set_mtime=not args.no_set_mtime,
            save_metadata=args.save_metadata,
            write_caption=args.write_caption,
            gps=gps,
            folder_name=args.folder_name or None,
        )
