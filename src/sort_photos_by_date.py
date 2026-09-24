"""사진/동영상을 촬영 날짜별 YYYY-MM-DD 폴더로 재분류하는 스크립트.

사용 예 (Windows 명령 프롬프트 / PowerShell):
    python src\\sort_photos_by_date.py "G:\\사진\\가족\\서리니"            # 미리보기(파일 이동 없음)
    python src\\sort_photos_by_date.py "G:\\사진\\가족\\서리니" --apply    # 실제로 이동
    python src\\sort_photos_by_date.py --undo "G:\\사진\\가족\\서리니\\_정리기록\\이동기록_....csv"

촬영 날짜는 다음 순서로 판단한다.
    1. 사진 EXIF 촬영일시 (JPG/TIFF/DNG, Pillow 가 설치되어 있으면 HEIC 등도)
    2. 파일 이름에 들어있는 날짜 (예: 20250312_143000.jpg, KakaoTalk_20250312_...)
    3. 동영상 메타데이터 생성일시 (MP4/MOV)
    4. 원래 있던 폴더 이름의 날짜 (예: 2025.03.12, 2025년 3월 12일)
    5. 파일 수정 날짜
외부 라이브러리 없이 동작하며, 파일은 절대 삭제하지 않는다.
"""

import argparse
import csv
import datetime as dt
import hashlib
import os
import re
import shutil
import struct
import sys

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".mts", ".m2ts", ".wmv"}
MEDIA_EXTS = PHOTO_EXTS | VIDEO_EXTS
# 사진과 이름이 같으면 함께 이동하는 부속 파일 (아이폰 편집정보, 라이트룸 등)
SIDECAR_EXTS = {".aae", ".xmp"}
# 폴더가 비었는지 판단할 때 무시하는 윈도우/맥 자동 생성 파일
JUNK_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}

DATE_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOG_DIR_NAME = "_정리기록"
DUP_DIR_NAME = "_중복"
MIN_DATE = dt.date(2000, 1, 1)

FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})([-_.]?)(0[1-9]|1[0-2])\2(0[1-9]|[12]\d|3[01])"
)
FOLDER_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*[-_. 년]\s*(\d{1,2})\s*[-_. 월]\s*(\d{1,2})(?!\d)"
)
COMPACT_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")


def make_date(year, month, day):
    """유효한 날짜이면 date, 아니면 None."""
    try:
        d = dt.date(int(year), int(month), int(day))
    except ValueError:
        return None
    if d < MIN_DATE or d > dt.date.today() + dt.timedelta(days=1):
        return None
    return d


# ---------------------------------------------------------------- EXIF

def _parse_exif_datetime(text):
    m = re.match(r"\s*(\d{4})[:\-](\d{2})[:\-](\d{2})", text)
    return make_date(*m.groups()) if m else None


def _read_tiff_datetime(tiff):
    """TIFF 구조(EXIF 본문)에서 촬영일시를 찾는다."""
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if tiff[:2] == b"II" else ">"

    def read_ifd(offset):
        tags = {}
        if offset + 2 > len(tiff):
            return tags
        (count,) = struct.unpack_from(endian + "H", tiff, offset)
        for i in range(count):
            pos = offset + 2 + i * 12
            if pos + 12 > len(tiff):
                break
            tag, typ, num = struct.unpack_from(endian + "HHI", tiff, pos)
            if typ == 2:  # ASCII
                start = pos + 8 if num <= 4 else struct.unpack_from(endian + "I", tiff, pos + 8)[0]
                raw = tiff[start:start + num]
                tags[tag] = raw.split(b"\0", 1)[0].decode("ascii", "ignore")
            elif typ == 4:  # LONG
                tags[tag] = struct.unpack_from(endian + "I", tiff, pos + 8)[0]
        return tags

    try:
        ifd0 = read_ifd(struct.unpack_from(endian + "I", tiff, 4)[0])
        exif = read_ifd(ifd0[0x8769]) if isinstance(ifd0.get(0x8769), int) else {}
    except struct.error:
        return None
    # DateTimeOriginal -> DateTimeDigitized -> DateTime
    for tags, tag in ((exif, 0x9003), (exif, 0x9004), (ifd0, 0x0132)):
        value = tags.get(tag)
        if isinstance(value, str):
            d = _parse_exif_datetime(value)
            if d:
                return d
    return None


def read_exif_date(path):
    """JPEG/TIFF 계열 파일의 EXIF 촬영일."""
    try:
        with open(path, "rb") as f:
            data = f.read(512 * 1024)
    except OSError:
        return None
    if data[:4] in (b"II*\0", b"MM\0*"):
        return _read_tiff_datetime(data)
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xDA:  # 이미지 데이터 시작 → 더 이상 메타데이터 없음
            return None
        (seg_len,) = struct.unpack_from(">H", data, i + 2)
        if marker == 0xE1 and data[i + 4:i + 10] == b"Exif\0\0":
            return _read_tiff_datetime(data[i + 10:i + 2 + seg_len])
        i += 2 + seg_len
    return None


def read_pillow_date(path):
    """Pillow(+pillow-heif)가 설치되어 있으면 HEIC/PNG 등의 EXIF 도 읽는다."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            values = [exif.get_ifd(0x8769).get(0x9003), exif.get_ifd(0x8769).get(0x9004), exif.get(0x0132)]
    except Exception:
        return None
    for value in values:
        if isinstance(value, str):
            d = _parse_exif_datetime(value)
            if d:
                return d
    return None


# ---------------------------------------------------------------- 동영상

def read_video_date(path):
    """MP4/MOV 의 moov/mvhd 생성시각(UTC)을 현지 날짜로 변환."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            moov = _find_atom(f, 0, size, b"moov")
            if not moov:
                return None
            mvhd = _find_atom(f, moov[0], moov[1], b"mvhd")
            if not mvhd:
                return None
            f.seek(mvhd[0])
            head = f.read(12)
    except OSError:
        return None
    if len(head) < 8:
        return None
    if head[0] == 1 and len(head) >= 12:
        (seconds,) = struct.unpack(">Q", head[4:12])
    else:
        (seconds,) = struct.unpack(">I", head[4:8])
    if seconds == 0:
        return None
    try:
        utc = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)
        local = utc.astimezone()
    except (OverflowError, OSError, ValueError):
        return None
    return make_date(local.year, local.month, local.day)


def _find_atom(f, start, end, name):
    """[start, end) 범위에서 atom 을 찾아 (본문 시작, 본문 끝) 반환."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            return None
        atom_size, atom_type = struct.unpack(">I4s", header)
        body = pos + 8
        if atom_size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return None
            (atom_size,) = struct.unpack(">Q", ext)
            body += 8
        elif atom_size == 0:
            atom_size = end - pos
        if atom_size < body - pos:
            return None
        if atom_type == name:
            return body, pos + atom_size
        pos += atom_size
    return None


# ---------------------------------------------------------------- 이름에서 날짜

def date_from_filename(name):
    stem = os.path.splitext(name)[0]
    for m in FILENAME_DATE_RE.finditer(stem):
        d = make_date(m.group(1), m.group(3), m.group(4))
        if d:
            return d
    return None


def date_from_folder_name(name):
    m = FOLDER_DATE_RE.search(name) or COMPACT_DATE_RE.search(name)
    if not m:
        return None
    return make_date(*m.groups()[:3])


def detect_date(path, root):
    """(날짜, 판단근거) 반환."""
    ext = os.path.splitext(path)[1].lower()
    if ext in PHOTO_EXTS:
        d = read_exif_date(path) or read_pillow_date(path)
        if d:
            return d, "EXIF 촬영일"
    d = date_from_filename(os.path.basename(path))
    if d:
        return d, "파일이름"
    if ext in VIDEO_EXTS:
        d = read_video_date(path)
        if d:
            return d, "동영상 메타데이터"
    parent = os.path.dirname(path)
    while os.path.normcase(parent) != os.path.normcase(root) and len(parent) > len(root):
        d = date_from_folder_name(os.path.basename(parent))
        if d:
            return d, "기존 폴더이름"
        parent = os.path.dirname(parent)
    mtime = dt.datetime.fromtimestamp(os.path.getmtime(path))
    return mtime.date(), "파일 수정날짜(확인 필요)"


# ---------------------------------------------------------------- 계획/실행

def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def same_content(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        return file_hash(a) == file_hash(b)
    except OSError:
        return False


def unique_path(path, taken):
    stem, ext = os.path.splitext(path)
    n = 1
    candidate = path
    while os.path.exists(candidate) or os.path.normcase(candidate) in taken:
        candidate = "{} ({}){}".format(stem, n, ext)
        n += 1
    return candidate


def scan(root):
    """분류 대상 미디어 파일, 부속 파일, 기타 파일 목록."""
    media, sidecars, others = [], [], []
    for dirpath, dirnames, filenames in os.walk(root):
        # _정리기록, _중복 등 밑줄로 시작하는 폴더는 건너뜀
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("_"))
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            ext = os.path.splitext(name)[1].lower()
            if ext in MEDIA_EXTS:
                media.append(path)
            elif ext in SIDECAR_EXTS:
                sidecars.append(path)
            elif name.lower() not in JUNK_FILES:
                others.append(path)
    return media, sidecars, others


def build_plan(root):
    root = os.path.abspath(root)
    media, sidecars, others = scan(root)
    plan = []
    taken = set()
    sidecar_map = {}
    for s in sidecars:
        key = os.path.normcase(os.path.splitext(s)[0])
        sidecar_map.setdefault(key, []).append(s)

    for src in media:
        date, reason = detect_date(src, root)
        folder = date.strftime("%Y-%m-%d")
        dst = os.path.join(root, folder, os.path.basename(src))
        action = "이동"
        if os.path.normcase(dst) == os.path.normcase(src):
            action = "그대로"
        elif os.path.exists(dst) or os.path.normcase(dst) in taken:
            if os.path.exists(dst) and same_content(src, dst):
                action = "중복"
                dst = unique_path(os.path.join(root, DUP_DIR_NAME, folder, os.path.basename(src)), taken)
            else:
                dst = unique_path(dst, taken)
        taken.add(os.path.normcase(dst))
        plan.append({"원래위치": src, "새위치": dst, "날짜": folder, "판단근거": reason, "작업": action})

        # 같은 이름의 부속 파일(.AAE 등)은 사진을 따라감
        for side in sidecar_map.pop(os.path.normcase(os.path.splitext(src)[0]), []):
            side_dst = os.path.splitext(dst)[0] + os.path.splitext(side)[1]
            side_action = "그대로" if os.path.normcase(side_dst) == os.path.normcase(side) else "이동"
            if side_action == "이동":
                side_dst = unique_path(side_dst, taken)
            taken.add(os.path.normcase(side_dst))
            plan.append({"원래위치": side, "새위치": side_dst, "날짜": folder,
                         "판단근거": "사진 부속파일", "작업": side_action})

    leftovers = others + [p for items in sidecar_map.values() for p in items]
    return plan, sorted(leftovers)


def old_folders_with_labels(root, plan):
    """이름에 날짜 외 설명(예: '백일')이 붙어 있던 폴더 목록 — 이름이 사라지므로 알려줌."""
    labels = set()
    for row in plan:
        if row["작업"] == "그대로":
            continue
        rel = os.path.relpath(os.path.dirname(row["원래위치"]), root)
        if rel == ".":
            continue
        for part in rel.split(os.sep):
            if not DATE_FOLDER_RE.match(part):
                labels.add(rel)
                break
    return sorted(labels)


def write_log(path, plan):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["작업", "날짜", "판단근거", "원래위치", "새위치"])
        writer.writeheader()
        writer.writerows(plan)


def apply_plan(plan):
    moved = 0
    for row in plan:
        if row["작업"] == "그대로":
            continue
        os.makedirs(os.path.dirname(row["새위치"]), exist_ok=True)
        if os.path.exists(row["새위치"]):
            raise FileExistsError(row["새위치"])
        shutil.move(row["원래위치"], row["새위치"])
        row["결과"] = "완료"
        moved += 1
    return moved


def remove_empty_dirs(root):
    """비어 있는(또는 Thumbs.db 등만 남은) 하위 폴더 정리. 파일은 지우지 않는다."""
    removed = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if os.path.normcase(dirpath) == os.path.normcase(root):
            continue
        if os.path.basename(dirpath).startswith("_"):
            continue
        entries = os.listdir(dirpath)
        if entries and not all(e.lower() in JUNK_FILES for e in entries):
            continue
        for e in entries:
            os.remove(os.path.join(dirpath, e))
        os.rmdir(dirpath)
        removed.append(dirpath)
    return removed


def undo(log_path):
    with open(log_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    restored = 0
    for row in reversed(rows):
        if row["작업"] == "그대로" or not os.path.exists(row["새위치"]):
            continue
        if os.path.exists(row["원래위치"]):
            print("  건너뜀(원래 위치에 이미 파일 있음): " + row["원래위치"])
            continue
        os.makedirs(os.path.dirname(row["원래위치"]), exist_ok=True)
        shutil.move(row["새위치"], row["원래위치"])
        restored += 1
    return restored


def print_summary(root, plan, leftovers):
    counts = {}
    for row in plan:
        counts[row["작업"]] = counts.get(row["작업"], 0) + 1
    reasons = {}
    for row in plan:
        if row["판단근거"] != "사진 부속파일":
            reasons[row["판단근거"]] = reasons.get(row["판단근거"], 0) + 1
    dates = sorted({row["날짜"] for row in plan})

    print("\n=== 요약 ===")
    print("대상 파일: {}개  (이동 {}, 이미 제자리 {}, 중복 {})".format(
        len(plan), counts.get("이동", 0), counts.get("그대로", 0), counts.get("중복", 0)))
    print("날짜 폴더: {}개".format(len(dates)))
    print("날짜 판단근거:")
    for reason, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print("  - {}: {}개".format(reason, n))
    if reasons.get("파일 수정날짜(확인 필요)"):
        print("  ※ '파일 수정날짜'로 분류된 파일은 복사한 날짜일 수 있으니 기록 파일에서 확인해 주세요.")
    labels = old_folders_with_labels(root, plan)
    if labels:
        print("\n이름에 설명이 붙어 있던 기존 폴더 (새 폴더 이름은 날짜만 남음):")
        for label in labels:
            print("  - " + label)
    if leftovers:
        print("\n사진/동영상이 아니라서 그대로 둔 파일 {}개:".format(len(leftovers)))
        for p in leftovers[:20]:
            print("  - " + os.path.relpath(p, root))
        if len(leftovers) > 20:
            print("  ... 외 {}개".format(len(leftovers) - 20))


def main(argv=None):
    parser = argparse.ArgumentParser(description="사진을 촬영 날짜별 YYYY-MM-DD 폴더로 재분류합니다.")
    parser.add_argument("root", nargs="?", help="정리할 폴더 (예: G:\\사진\\가족\\서리니)")
    parser.add_argument("--apply", action="store_true", help="실제로 파일을 이동 (없으면 미리보기만)")
    parser.add_argument("--undo", metavar="기록파일.csv", help="이전에 --apply 한 이동을 되돌림")
    args = parser.parse_args(argv)

    if args.undo:
        n = undo(args.undo)
        print("{}개 파일을 원래 위치로 되돌렸습니다.".format(n))
        return 0
    if not args.root:
        parser.error("정리할 폴더를 지정해 주세요.")
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        parser.error("폴더를 찾을 수 없습니다: " + root)

    print("폴더 분석 중: " + root)
    plan, leftovers = build_plan(root)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = os.path.join(root, LOG_DIR_NAME)

    for row in plan:
        if row["작업"] != "그대로":
            print("[{}] {}  ->  {}   ({})".format(
                row["작업"], os.path.relpath(row["원래위치"], root),
                os.path.relpath(row["새위치"], root), row["판단근거"]))
    print_summary(root, plan, leftovers)

    if not args.apply:
        log_path = os.path.join(log_dir, "미리보기_{}.csv".format(stamp))
        write_log(log_path, plan)
        print("\n[미리보기] 파일은 이동하지 않았습니다. 계획표: " + log_path)
        print("문제 없으면 --apply 를 붙여 다시 실행하세요.")
        return 0

    log_path = os.path.join(log_dir, "이동기록_{}.csv".format(stamp))
    write_log(log_path, plan)  # 이동 전에 먼저 기록 → 중간에 멈춰도 되돌리기 가능
    moved = apply_plan(plan)
    removed = remove_empty_dirs(root)
    print("\n완료: {}개 파일 이동, 빈 폴더 {}개 정리.".format(moved, len(removed)))
    print("이동 기록: " + log_path)
    print('되돌리려면: python sort_photos_by_date.py --undo "{}"'.format(log_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
