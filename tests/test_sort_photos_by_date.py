import os
import struct
import sys
import tempfile
import unittest
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sort_photos_by_date as spd  # noqa: E402


def make_jpeg_with_exif(path, when="2025:03:12 14:30:00"):
    """DateTimeOriginal 만 가진 최소 JPEG(EXIF) 파일 생성."""
    value = when.encode("ascii") + b"\0"  # 20 bytes
    # TIFF 헤더(8) + IFD0(2 + 12 + 4) = 26 → Exif IFD 시작
    exif_ifd_offset = 26
    string_offset = exif_ifd_offset + 2 + 12 + 4
    tiff = b"II*\0" + struct.pack("<I", 8)
    tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x8769, 4, 1, exif_ifd_offset) + struct.pack("<I", 0)
    tiff += struct.pack("<H", 1) + struct.pack("<HHII", 0x9003, 2, len(value), string_offset) + struct.pack("<I", 0)
    tiff += value
    app1 = b"Exif\0\0" + tiff
    data = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xda\0\x02" + b"\xff\xd9"
    with open(path, "wb") as f:
        f.write(data)


def make_mp4(path, when_utc):
    seconds = int((when_utc - dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)).total_seconds())
    mvhd_body = b"\0\0\0\0" + struct.pack(">II", seconds, seconds) + b"\0" * 88
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0"
    mdat = struct.pack(">I4s", 16, b"mdat") + b"\0" * 8
    with open(path, "wb") as f:
        f.write(ftyp + mdat + moov)


def touch(path, content=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    if mtime:
        ts = dt.datetime(*mtime).timestamp()
        os.utime(path, (ts, ts))


class DateDetectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_exif(self):
        p = os.path.join(self.root, "photo.jpg")
        make_jpeg_with_exif(p)
        self.assertEqual(spd.read_exif_date(p), dt.date(2025, 3, 12))

    def test_mp4(self):
        p = os.path.join(self.root, "clip.mp4")
        make_mp4(p, dt.datetime(2025, 6, 1, 12, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(spd.read_video_date(p), dt.date(2025, 6, 1))

    def test_filenames(self):
        cases = {
            "20250312_143000.jpg": dt.date(2025, 3, 12),
            "IMG_20250312_143000.jpg": dt.date(2025, 3, 12),
            "KakaoTalk_Photo_2025-03-12-14-30-00.jpeg": dt.date(2025, 3, 12),
            "Screenshot_2025.11.02.png": dt.date(2025, 11, 2),
            "IMG_1234.JPG": None,
            "20251399.jpg": None,
        }
        for name, expected in cases.items():
            self.assertEqual(spd.date_from_filename(name), expected, name)

    def test_folder_names(self):
        self.assertEqual(spd.date_from_folder_name("2025.03.12 백일"), dt.date(2025, 3, 12))
        self.assertEqual(spd.date_from_folder_name("2025년 3월 2일"), dt.date(2025, 3, 2))
        self.assertEqual(spd.date_from_folder_name("20250312"), dt.date(2025, 3, 12))
        self.assertIsNone(spd.date_from_folder_name("신생아"))


class PlanApplyUndoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def test_full_flow(self):
        os.makedirs(self.p("2025.03 백일"))
        make_jpeg_with_exif(self.p("2025.03 백일", "IMG_0001.jpg"))            # EXIF → 2025-03-12
        touch(self.p("2025.03 백일", "IMG_0001.AAE"))                           # 부속 파일
        touch(self.p("잡동사니", "20250401_100000.jpg"), b"a")                  # 파일이름
        touch(self.p("2025년 5월 5일", "IMG_0002.jpg"), b"b")                   # 폴더이름
        touch(self.p("2025-05-05", "IMG_0002.jpg"), b"b")                       # 같은 내용 → 중복
        touch(self.p("etc", "IMG_0003.jpg"), b"c", mtime=(2025, 7, 1, 9, 0))   # 수정날짜
        touch(self.p("etc", "메모.txt"), b"memo")                              # 사진 아님
        touch(self.p("etc", "Thumbs.db"))

        plan, leftovers = spd.build_plan(self.root)
        by_src = {os.path.relpath(r["원래위치"], self.root): r for r in plan}

        self.assertEqual(by_src[os.path.join("2025.03 백일", "IMG_0001.jpg")]["날짜"], "2025-03-12")
        self.assertEqual(by_src[os.path.join("2025.03 백일", "IMG_0001.AAE")]["새위치"],
                         self.p("2025-03-12", "IMG_0001.AAE"))
        self.assertEqual(by_src[os.path.join("잡동사니", "20250401_100000.jpg")]["판단근거"], "파일이름")
        self.assertEqual(by_src[os.path.join("2025-05-05", "IMG_0002.jpg")]["작업"], "그대로")
        self.assertEqual(by_src[os.path.join("2025년 5월 5일", "IMG_0002.jpg")]["작업"], "중복")
        self.assertEqual(by_src[os.path.join("etc", "IMG_0003.jpg")]["날짜"], "2025-07-01")
        self.assertEqual([os.path.basename(x) for x in leftovers], ["메모.txt"])

        log = self.p("_정리기록", "log.csv")
        spd.write_log(log, plan)
        spd.apply_plan(plan)
        spd.remove_empty_dirs(self.root)

        self.assertTrue(os.path.exists(self.p("2025-03-12", "IMG_0001.jpg")))
        self.assertTrue(os.path.exists(self.p("2025-04-01", "20250401_100000.jpg")))
        self.assertTrue(os.path.exists(self.p("_중복", "2025-05-05", "IMG_0002.jpg")))
        self.assertTrue(os.path.exists(self.p("2025-07-01", "IMG_0003.jpg")))
        self.assertFalse(os.path.exists(self.p("2025.03 백일")))
        self.assertFalse(os.path.exists(self.p("잡동사니")))
        self.assertTrue(os.path.exists(self.p("etc", "메모.txt")))

        # 다시 실행하면 옮길 것이 없어야 함
        plan2, _ = spd.build_plan(self.root)
        self.assertTrue(all(r["작업"] == "그대로" for r in plan2))

        spd.undo(log)
        self.assertTrue(os.path.exists(self.p("2025.03 백일", "IMG_0001.jpg")))
        self.assertTrue(os.path.exists(self.p("2025.03 백일", "IMG_0001.AAE")))
        self.assertTrue(os.path.exists(self.p("2025년 5월 5일", "IMG_0002.jpg")))
        self.assertTrue(os.path.exists(self.p("etc", "IMG_0003.jpg")))

    def test_name_collision_different_content(self):
        touch(self.p("a", "20250101_000000.jpg"), b"one")
        touch(self.p("b", "20250101_000000.jpg"), b"two")
        plan, _ = spd.build_plan(self.root)
        targets = sorted(os.path.basename(r["새위치"]) for r in plan)
        self.assertEqual(targets, ["20250101_000000 (1).jpg", "20250101_000000.jpg"])
        spd.apply_plan(plan)
        self.assertEqual(len(os.listdir(self.p("2025-01-01"))), 2)


if __name__ == "__main__":
    unittest.main()
