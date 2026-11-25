import copy
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from cvs.common import UserError
from cvs.doctor import backend
from cvs.docs import read_document, section_paragraphs
from cvs.plan import validate_edit, video_bitrate
from cvs.transcribe import filter_hallucinations


def ffmpeg(*args, check=True):
    return subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y", *map(str, args)],
                          capture_output=True, encoding="utf-8", errors="replace", check=check)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class UnitTests(unittest.TestCase):
    def test_backend(self):
        self.assertEqual(backend("Darwin", "arm64"), "mlx_whisper")
        self.assertEqual(backend("Darwin", "x86_64"), "faster_whisper")
        self.assertEqual(backend("Windows", "AMD64"), "faster_whisper")
        self.assertEqual(backend("Linux", "aarch64"), "faster_whisper")

    def test_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hwpx, docx, text = (root / name for name in ("대회 요강.hwpx", "수업 계획.docx", "요건.txt"))
            with zipfile.ZipFile(hwpx, "w") as archive:
                for i, value in [(10, "마지막"), (2, "처음")]:
                    archive.writestr(f"Contents/section{i}.xml", f"""
                    <root xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
                      <hp:p><hp:run><hp:t>{value}</hp:t></hp:run></hp:p>
                    </root>""")
            with zipfile.ZipFile(docx, "w") as archive:
                archive.writestr("word/document.xml", """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:r><w:t>제출 </w:t><w:t>요건</w:t><w:br/><w:t>10분 이내</w:t></w:r></w:p>
                    <w:p><w:r><w:t>두 번째 문단</w:t></w:r></w:p>
                  </w:body>
                </w:document>""")
            text.write_bytes("시험 안내".encode("cp949"))
            self.assertEqual(read_document(hwpx), "처음\n마지막")
            self.assertEqual(read_document(docx), "제출 요건\n10분 이내\n두 번째 문단")
            self.assertEqual(read_document(text), "시험 안내")

    def test_hwp_record_control_codes(self):
        payload = ("가".encode("utf-16-le") + b"\x01\x00" + b"A" * 14
                   + "나\n다\r라".encode("utf-16-le"))
        header = struct.pack("<II", (0xFFF << 20) | 67, len(payload))
        self.assertEqual(list(section_paragraphs(header + payload)), ["가나\n다\n라"])

    def test_hallucinations(self):
        text = ["수업을 시작해요.", "수업을 시작해요", "수업을  시작해요!",
                "시청해 주셔서 감사합니다.", "다음 문제를 풀어요.", "시청해 주셔서 감사합니다."]
        rows = [{"src": "A", "t0": i, "t1": i + 1, "text": value} for i, value in enumerate(text)]
        kept, removed = filter_hallucinations(rows, {"A": [(3, 4)]})
        self.assertEqual(kept, [rows[0], rows[4], rows[5]])
        self.assertEqual(removed, 3)

    def test_plan_validation(self):
        sources = [{"id": "A", "duration": 20, "offset": 0},
                   {"id": "B", "duration": 15, "offset": 20}]
        plan = {"segments": [{"id": "c01", "src": "A", "start": 0, "end": 5},
                             {"id": "c02", "src": "A", "start": 5, "end": 8},
                             {"id": "c03", "src": "B", "start": 0, "end": 8, "speed": 4}]}
        self.assertEqual(validate_edit(plan, sources)["segments"][0]["speed"], 1)
        for index, update in [(1, {"start": 4}), (2, {"src": "A"}), (2, {"speed": 9}),
                              (2, {"speed": 1.5}), (2, {"id": "../bad"})]:
            with self.subTest(update=update):
                bad = copy.deepcopy(plan)
                bad["segments"][index].update(update)
                with self.assertRaises(UserError):
                    validate_edit(bad, sources)

    def test_fit_bitrate(self):
        self.assertEqual(video_bitrate(500, 600), 6_205_333)
        with self.assertRaises(UserError):
            video_bitrate(1, 600)

    def test_teacher_zone_during_crossfade(self):
        from cvs.check import outside_teacher
        from cvs.faces import faces_to_blur
        left, right = [(0, 0, 0.5, 1)], [(0.5, 0, 1, 1)]
        teacher = (0.7, 0.4, 0.75, 0.45)
        # 두 컷이 겹치는 순간: 뒤 컷의 교사 칸에 든 얼굴은 의심이 아니다
        self.assertEqual(outside_teacher([teacher], [left, right], faces_to_blur), [])
        # 한 컷만 보일 때: 그 컷 교사 칸 밖이면 의심이다
        self.assertEqual(outside_teacher([teacher], [left], faces_to_blur), [teacher])
        self.assertEqual(outside_teacher([teacher], [], faces_to_blur), [teacher])

    def test_only_top_face_in_teacher_cell_is_kept(self):
        from cvs.faces import faces_to_blur
        cell = [(2 / 3, 1 / 3, 1.0, 2 / 3)]  # C2
        teacher = (0.80, 0.36, 0.84, 0.42)        # 서 있는 교사: 칸 안에서 가장 위
        teacher_again = (0.805, 0.365, 0.845, 0.425)  # 이어 잡기로 생긴 같은 얼굴 상자
        student = (0.92, 0.58, 0.97, 0.64)        # 같은 칸에 앉은 학생
        outside = (0.10, 0.70, 0.14, 0.76)        # 칸 밖 학생
        blurred = faces_to_blur([teacher, teacher_again, student, outside], cell)
        self.assertEqual(blurred, [student, outside])
        self.assertEqual(faces_to_blur([student], []), [student])

    def test_blur_cells(self):
        from cvs.plan import mask_regions, validate_options
        options = validate_options({"blur_cells": ["A3", "C3"], "masks": [[0.1, 0.1, 0.2, 0.2]]})
        self.assertEqual(mask_regions(options),
                         [(0.1, 0.1, 0.2, 0.2), (0, 2 / 3, 1 / 3, 1.0), (2 / 3, 2 / 3, 1.0, 1.0)])
        with self.assertRaises(UserError):
            validate_options({"blur_cells": ["D1"]})

    def test_privacy_view_keeps_only_label_areas(self):
        import numpy as np
        from cvs.faces import privacy_view
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, (176, 320, 3), dtype=np.uint8)
        original = frame.copy()
        privacy_view(frame, [(0, 0, 0.5, 0.5)])
        self.assertTrue((frame[:88, :160] == original[:88, :160]).all())
        # 남기지 않은 곳은 16픽셀 칸마다 한 색이어야 한다(얼굴을 알아볼 수 없게)
        block = frame[96:112, 176:192]
        self.assertTrue((block == block[0, 0]).all())
        self.assertFalse((frame[88:, 160:] == original[88:, 160:]).all())

    def test_teacher_centers_and_interpolation(self):
        from cvs.faces import is_teacher, tracked_frames
        from cvs.plan import teacher_regions

        left = (0.28, 0.1, 0.38, 0.2)
        right = (0.34, 0.1, 0.44, 0.2)

        class FixtureDetector:
            def find(self, frame):
                return {0: [left], 2: [right]}.get(frame, [])

        frames = dict(tracked_frames(range(7), FixtureDetector(), 10, 0.2))
        zones = teacher_regions({"teacher_cells": ["B1"]})
        self.assertFalse(is_teacher(frames[0][0], zones))
        self.assertAlmostEqual(frames[1][0][0], 0.31)
        self.assertTrue(is_teacher(frames[2][0], zones))
        self.assertEqual(frames[6], [right])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg와 ffprobe가 필요해요.")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures = tempfile.TemporaryDirectory(prefix="cvs-fixtures-")
        cls.addClassCleanup(fixtures.cleanup)
        cls.fixtures = Path(fixtures.name)
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=256x144:rate=30:duration=20",
               "-f", "lavfi", "-i", "sine=frequency=420:sample_rate=48000:duration=6",
               "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=5",
               "-f", "lavfi", "-i", "sine=frequency=420:sample_rate=48000:duration=9",
               "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]",
               "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
               "-c:a", "aac", cls.fixtures / "IMG_2 원본.MOV")
        unrotated = cls.fixtures / "rotation.mp4"
        rotated = cls.fixtures / "IMG_10 세로.MOV"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=256x144:rate=30:duration=15",
               "-f", "lavfi", "-i", "sine=frequency=680:sample_rate=48000:duration=15",
               "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
               "-c:a", "aac", unrotated)
        rotation = ffmpeg("-display_rotation", "90", "-i", unrotated, "-c", "copy", rotated, check=False)
        if rotation.returncode:
            ffmpeg("-i", unrotated, "-c", "copy", "-metadata:s:v:0", "rotate=90", rotated)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cvs-test-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name) / "한글 작업 폴더"
        self.cli("init")
        self.plan = self.work / "plan"
        for path in self.fixtures.glob("IMG_*.MOV"):
            shutil.copyfile(path, self.work / "원본" / path.name)
        self.cli("probe")

    def cli(self, command, *args, expected=0):
        result = subprocess.run([sys.executable, str(TOOLS / "cvs.py"), "--debug", command,
                                 str(self.work), *map(str, args)],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=90)
        if expected:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        else:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def expect_failure(self, command, *args):
        result = subprocess.run([sys.executable, str(TOOLS / "cvs.py"), command, str(self.work), *map(str, args)],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=90)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return result.stdout

    def make_plan(self):
        plan = load(self.plan / "편집안.json")
        plan["title"] = "가짜 수업"
        plan["segments"] = [
            {"id": "c01", "src": "A", "start": 1, "end": 3, "stage": "도입", "note": "실험 소개"},
            {"id": "c02", "src": "A", "start": 6, "end": 10, "stage": "전개 1", "note": "모둠 활동", "speed": 4},
            {"id": "c03", "src": "B", "start": 1, "end": 3, "stage": "전개 1", "masks": [[0.1, 0.1, 0.2, 0.2]]},
        ]
        save(self.plan / "편집안.json", plan)
        return plan

    def test_probe_and_fetch(self):
        result = load(self.plan / "원본.json")
        first, second = result["sources"]
        self.assertEqual((first["id"], second["id"]), ("A", "B"))
        self.assertEqual((first["file"], second["file"]), ("원본/IMG_2 원본.MOV", "원본/IMG_10 세로.MOV"))
        self.assertEqual((first["width"], first["height"]), (256, 144))
        self.assertEqual((second["width"], second["height"]), (144, 256))
        self.assertIn(second["rotation"], (90, 270))
        self.assertAlmostEqual(second["offset"], 20, places=2)
        self.assertAlmostEqual(result["total"], 35, places=2)
        document = self.work.parent / "요강 문서.txt"
        document.write_text("가짜 안내", encoding="utf-8")
        new = self.work.parent / "추가 영상.MOV"
        shutil.copyfile(self.fixtures / "IMG_2 원본.MOV", new)
        self.cli("fetch", document, new)
        self.assertTrue(new.is_file())
        self.assertTrue((self.work / "원본" / new.name).is_file())
        self.assertFalse((self.work / "요강" / document.name).exists())
        self.cli("fetch", new, "--move")
        self.assertTrue(new.is_file())
        moved = self.work.parent / "옮길 영상.MOV"
        shutil.copyfile(new, moved)
        self.cli("fetch", moved, "--move")
        self.assertFalse(moved.exists())
        self.assertTrue((self.work / "원본" / moved.name).is_file())

    def test_analyze(self):
        save(self.plan / "받아쓰기.json", [{"start": 1, "end": 4}, {"start": 3, "end": 5}])
        self.cli("analyze")
        result = load(self.plan / "분석.json")
        low, high = result["silences"][0]
        self.assertAlmostEqual(low, 6, delta=0.15)
        self.assertAlmostEqual(high, 11, delta=0.15)
        self.assertEqual(len(result["loudness"]), 2)
        self.assertIsInstance(result["loudness"][0]["lufs"], float)
        self.assertAlmostEqual(result["speech_ratio"][0]["ratio"], 4 / 30, places=4)

    def test_conti_and_notes(self):
        self.make_plan()
        self.cli("conti")
        script = load(self.work / "script" / "V1.json")
        self.assertEqual(script[1]["display"], "[전개 1] 00:06–00:10 · 모둠 활동 · 4배속")
        self.assertEqual(script[0]["role"], "narrator")
        self.assertNotIn("sectionTitle", script[2])
        path = self.work / "conti" / "V1.json"
        conti = load(path)
        self.assertEqual((conti["id"], conti["shape"]), ("V1", "wide"))
        element = conti["cuts"][0]["els"][0]
        self.assertEqual((element["t"], element["src"], element["pos"], element["from"], element["at"]),
                         ("vid", "clips/c01.mp4", [960, 540], 0, 1))
        width, height = (int(value) for value in element["aspect"].split(" / "))
        # 가로 콘티 안에 원본 방향 그대로 들어가야 한다(높이 1080, 폭 1920 이하)
        self.assertLessEqual(element["h"], 1080)
        self.assertLessEqual(element["h"] * width / height, 1920 + 1)
        timeline = load(self.work / "conti" / "V1-timeline.json")
        self.assertAlmostEqual(timeline["duration"], 4.2)
        self.assertEqual(timeline["lines"][1], {"n": 2, "start": 1.6, "end": 2.6})
        clip = self.work / "media" / "clips" / "c02.mp4"
        before = clip.stat().st_mtime_ns
        conti["cuts"][1].update(memo="앞부분을 조금 늘려 주세요.", status="fix")
        conti["cuts"].pop()
        save(path, conti)
        self.cli("conti-notes")
        notes = load(self.plan / "콘티메모.json")
        self.assertEqual([(item["id"], item["missing"]) for item in notes], [("c02", False), ("c03", True)])
        self.cli("conti")
        saved = load(path)
        self.assertEqual(saved["cuts"][1]["memo"], "앞부분을 조금 늘려 주세요.")
        self.assertEqual(saved["cuts"][1]["status"], "fix")
        self.assertEqual(before, clip.stat().st_mtime_ns)
        saved["cuts"][0].update(memo="이 컷은 그대로 둬요.")
        save(path, saved)
        self.cli("conti", "--resolved", "c02")
        resolved = load(path)
        self.assertEqual((resolved["cuts"][1]["memo"], resolved["cuts"][1]["status"]), ("", ""))
        self.assertEqual(resolved["cuts"][0]["memo"], "이 컷은 그대로 둬요.")
        self.cli("conti", "--reset-notes")
        self.assertEqual(load(path)["cuts"][1]["memo"], "")

    def test_faces_grid(self):
        from PIL import Image

        self.cli("faces-grid", "--every", "10")
        with Image.open(self.plan / "얼굴확인.jpg") as grid:
            self.assertEqual(grid.size, (1280, 242))

    def test_render_draft_and_check(self):
        from PIL import Image
        from cvs.common import run
        from cvs.media import source_info

        plan = self.make_plan()
        audio = self.work / "audio" / "해설 시험.wav"
        audio.parent.mkdir()
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=990:sample_rate=48000:duration=0.7", audio)
        plan["narration"] = [{"cut": "c01", "file": "audio/해설 시험.wav", "at": 0.2, "duck_db": -14}]
        save(self.plan / "편집안.json", plan)
        self.cli("render", "--draft")
        path = self.work / "out" / "가짜 수업_시안.mp4"
        self.assertTrue(path.is_file())
        info = source_info(path)
        self.assertAlmostEqual(info["duration"], 4.2, places=2)
        self.assertEqual((info["width"], info["height"], info["acodec"]), (640, 360, "aac"))
        self.assertEqual(info["fps"], 30)
        portrait = self.work / ".cache" / "seg" / "c03.mp4"

        def clock_pixels(time):
            return run(["ffmpeg", "-v", "error", "-ss", time, "-i", portrait,
                        "-frames:v", "1", "-vf", "crop=100:40:520:310",
                        "-pix_fmt", "gray", "-f", "rawvideo", "-"], "수업 시각 시험").stdout

        early, late = clock_pixels("0.5"), clock_pixels("1.5")
        self.assertGreater(max(early), 180)
        self.assertNotEqual(early, late)
        segment = self.work / ".cache" / "seg" / "c02.mp4"
        stamp = segment.stat().st_mtime_ns
        self.cli("render", "--draft")
        self.assertEqual(stamp, segment.stat().st_mtime_ns)
        conditions = load(self.plan / "조건표.json")
        conditions["duration"].update(min_sec=4, max_sec=5)
        conditions["format"].update(ext="mp4", vcodec="h264", resolution="640x360", fps=30)
        save(self.plan / "조건표.json", conditions)
        self.cli("check", path)
        self.assertIn("| 얼굴 점검 |", (self.work / "out" / "점검.md").read_text(encoding="utf-8"))
        with Image.open(self.work / "out" / "점검" / "모음.jpg") as board:
            self.assertEqual(board.size, (1280, 1210))
        conditions["duration"]["max_sec"] = 4.1
        save(self.plan / "조건표.json", conditions)
        self.assertIn("실패", self.expect_failure("check", path))

    def test_render_restrictions(self):
        self.make_plan()
        conditions = load(self.plan / "조건표.json")
        conditions["edit"].update(speedup=False, stage_label=False)
        save(self.plan / "조건표.json", conditions)
        output = self.expect_failure("render", "--draft")
        self.assertIn("빨리 감기", output)
        self.assertIn("수업 단계 꼬리표", output)
        self.assertFalse((self.work / ".cache" / "seg").exists())
        conditions["edit"].update(speedup=True, stage_label=True)
        save(self.plan / "조건표.json", conditions)
        self.assertIn("confirmed", self.expect_failure("render"))
        conditions["confirmed"] = True
        conditions["duration"]["max_sec"] = 4
        save(self.plan / "조건표.json", conditions)
        self.assertIn("최대 4초", self.expect_failure("render"))

    def test_submission_formats_and_file_limit(self):
        from cvs.media import source_info

        edit = self.make_plan()
        edit["segments"] = [{"id": "c01", "src": "A", "start": 0, "end": 1},
                            {"id": "c02", "src": "A", "start": 1, "end": 2}]
        edit["options"].update(blur_faces=False, clock_label=False, stage_label=False, crossfade=0)
        save(self.plan / "편집안.json", edit)
        conditions = load(self.plan / "조건표.json")
        conditions.update(confirmed=True, max_mb=0.24, filename="제출용 가짜 영상")
        conditions["format"].update(resolution="320x180", fps=30)
        cases = [("mp4", "h264", "aac"), ("mov", "h264", "aac"), ("mkv", "h264", "aac"),
                 ("avi", "mpeg4", "mp3"), ("wmv", "wmv2", "wmav2")]
        for ext, video, audio in cases:
            with self.subTest(ext=ext):
                conditions["format"].update(ext=ext, vcodec=video)
                save(self.plan / "조건표.json", conditions)
                self.cli("render")
                path = self.work / "out" / f"제출용 가짜 영상.{ext}"
                info = source_info(path)
                self.assertLessEqual(path.stat().st_size, 240_000)
                self.assertEqual((info["vcodec"], info["acodec"]), (video, audio))
                self.assertAlmostEqual(info["duration"], 2, places=2)

    def test_join_thirty_segments(self):
        from cvs.media import source_info

        edit = self.make_plan()
        edit["segments"] = [{"id": f"c{i+1:02}", "src": "A", "start": i / 2, "end": (i + 1) / 2}
                            for i in range(30)]
        edit["options"].update(blur_faces=False, clock_label=False, stage_label=False, crossfade=0.1)
        save(self.plan / "편집안.json", edit)
        conditions = load(self.plan / "조건표.json")
        conditions["format"].update(resolution="160x90", fps="30000/1001")
        save(self.plan / "조건표.json", conditions)
        self.cli("render", "--draft")
        path = self.work / "out" / "가짜 수업_시안.mp4"
        frames = 30 * 15 - 29 * 3
        self.assertAlmostEqual(source_info(path)["duration"], frames * 1001 / 30_000, places=3)


if __name__ == "__main__":
    unittest.main()
