import math
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .common import UserError, finite_number, local_path, read_json, say, write_json
from .media import fit_size, fraction, sources_for

CONDITIONS = {
    "contest": None, "source": None, "confirmed": False,
    "duration": {"min_sec": None, "max_sec": None, "target_sec": None},
    "format": {"ext": None, "vcodec": None, "resolution": None, "fps": None},
    "max_mb": None, "filename": None, "captions": None,
    "edit": {"speedup": None, "stage_label": None, "clock_label": None, "narration": None},
    "blur_faces": None, "anonymity": {"school": None, "teacher": None}, "notes": None,
}
OPTIONS = {
    "crossfade": 0.4, "stage_label": True, "clock_label": True,
    "blur_faces": True, "blur_style": "blur", "teacher_cells": [], "teacher_zones": [],
    "masks": [], "blur_cells": [], "loudnorm": -16,
}
CODECS = {
    "mp4": ("h264", "aac", "libx264", "aac"),
    "mov": ("h264", "aac", "libx264", "aac"),
    "mkv": ("h264", "aac", "libx264", "aac"),
    "avi": ("mpeg4", "mp3", "mpeg4", "libmp3lame"),
    "wmv": ("wmv2", "wmav2", "wmv2", "wmav2"),
}
V_CODEC_NAMES = {"libx264": "h264", "avc": "h264", "h.264": "h264", "mpeg-4": "mpeg4"}
WORK_DIRS = ("원본", "요강", "지도안", "plan", "conti", "script", "media", "stills", "out", ".cache")


def init(work):
    for directory in WORK_DIRS:
        (work / directory).mkdir(parents=True, exist_ok=True)
    seeds = {
        "조건표.json": CONDITIONS,
        "편집안.json": {"version": 1, "title": "", "options": OPTIONS,
                     "segments": [], "narration": []},
    }
    for name, value in seeds.items():
        path = work / "plan" / name
        if not path.exists():
            write_json(path, value)
    say(f"{work}에 작업 폴더를 준비했어요.")


def object_value(value, label):
    if not isinstance(value, dict):
        raise UserError(f"{label} 형식을 읽을 수 없어요. JSON 객체로 적어 주세요.")
    return value


def list_value(value, label):
    if not isinstance(value, list):
        raise UserError(f"{label} 형식을 읽을 수 없어요. JSON 목록으로 적어 주세요.")
    return value


def string_value(value, label):
    if not isinstance(value, str):
        raise UserError(f"{label} 값이 글자가 아니에요. 문자열로 적어 주세요.")
    return value


def switch(value, label, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, bool):
        raise UserError(f"{label} 값이 맞지 않아요. true 또는 false로 적어 주세요.")


def positive(value, label):
    if value is None:
        return None
    number = finite_number(value, label)
    if number <= 0:
        raise UserError(f"{label} 값이 너무 작아요. 0보다 큰 수로 적어 주세요.")
    return number


def rectangles(value, label):
    normalized = []
    for box in list_value(value, label):
        if isinstance(box, dict) and {"x", "y", "w", "h"} <= box.keys():
            x, y, w, h = (finite_number(box[key], label) for key in ("x", "y", "w", "h"))
            coords = (x, y, x + w, y + h)
        elif isinstance(box, list) and len(box) == 4:
            coords = tuple(finite_number(part, label) for part in box)
        else:
            raise UserError(f"{label} 상자 형식이 맞지 않아요. [왼쪽, 위, 오른쪽, 아래] 순서로 적어 주세요.")
        x0, y0, x1, y1 = coords
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise UserError(f"{label} 상자가 화면을 벗어나요. 0~1 사이로 적어 주세요.")
        normalized.append(coords)
    return normalized


def teacher_cells(value, label):
    cells = list_value(value, label)
    if any(not isinstance(cell, str) or not re.fullmatch(r"[ABC][123]", cell) for cell in cells):
        raise UserError(f"{label} 칸 이름이 맞지 않아요. A1부터 C3까지의 칸 이름을 적어 주세요.")
    return cells


def cell_rect(cell):
    column, row = ord(cell[0]) - ord("A"), int(cell[1]) - 1
    return (column / 3, row / 3, (column + 1) / 3, (row + 1) / 3)


def teacher_regions(options):
    return list(options.get("teacher_zones", [])) + [cell_rect(cell) for cell in options.get("teacher_cells", [])]


def mask_regions(options):
    # 늘 흐릴 곳: 손으로 적은 상자(masks) + 통째로 흐릴 칸(blur_cells, 예: 학생 자리)
    return list(options.get("masks", [])) + [cell_rect(cell) for cell in options.get("blur_cells", [])]


def validate_options(value, defaults=True):
    options = {**(OPTIONS if defaults else {}), **object_value(value, "편집 옵션")}
    for key in ("stage_label", "clock_label", "blur_faces"):
        if key in options:
            switch(options[key], key)
    if "crossfade" in options:
        options["crossfade"] = finite_number(options["crossfade"], "이어 붙이는 시간")
        if not 0 <= options["crossfade"] <= 60:
            raise UserError("이어 붙이는 시간이 범위를 벗어났어요. crossfade를 0~60초로 적어 주세요.")
    if "blur_style" in options and options["blur_style"] not in ("blur", "mosaic"):
        raise UserError("흐림 방식이 맞지 않아요. blur 또는 mosaic로 적어 주세요.")
    if "teacher_cells" in options:
        options["teacher_cells"] = teacher_cells(options["teacher_cells"], "교사 칸")
    if "blur_cells" in options:
        options["blur_cells"] = teacher_cells(options["blur_cells"], "흐릴 칸")
    for key in ("teacher_zones", "masks"):
        if key in options:
            options[key] = rectangles(options[key], key)
    if "loudnorm" in options:
        options["loudnorm"] = finite_number(options["loudnorm"], "목표 소리 크기")
        if not -70 <= options["loudnorm"] <= -5:
            raise UserError("목표 소리 크기가 범위를 벗어났어요. loudnorm을 -70~-5 LUFS로 적어 주세요.")
    return options


def validate_edit(value, sources, allow_empty=False):
    data = object_value(value, "편집안")
    if data.get("version", 1) != 1:
        raise UserError("이 편집안 버전을 읽을 수 없어요. version을 1로 적어 주세요.")
    title = string_value(data.get("title", ""), "영상 제목")
    options = validate_options(data.get("options", {}))
    sources_by_id = {source["id"]: source for source in sources}
    segments = []
    last_end = 0.0
    seen = set()
    for item in list_value(data.get("segments", []), "편집 구간"):
        segment = object_value(item, "편집 구간").copy()
        cut = string_value(segment.get("id"), "컷 ID")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", cut)
                or cut.casefold() in seen or reserved_filename(cut)):
            raise UserError("컷 ID가 중복되거나 파일 이름에 쓸 수 없어요. c01처럼 서로 다른 ID를 적어 주세요.")
        seen.add(cut.casefold())
        src = string_value(segment.get("src"), "원본 ID")
        source = sources_by_id.get(src)
        if source is None:
            raise UserError(f"{cut}의 원본을 찾을 수 없어요. 원본.json의 ID를 적어 주세요.")
        start = finite_number(segment.get("start"), f"{cut} 시작")
        end = finite_number(segment.get("end"), f"{cut} 끝")
        speed = finite_number(segment.get("speed", 1), f"{cut} 재생 속도")
        if not (speed == 1 or 2 <= speed <= 8):
            raise UserError(f"{cut} 재생 속도가 범위를 벗어났어요. 1 또는 2~8배속을 적어 주세요.")
        if not 0 <= start < end <= source["duration"]:
            raise UserError(f"{cut}의 시각이 원본 범위를 벗어났어요. 원본 안에서 시작과 끝을 확인해 주세요.")
        if start + source["offset"] < last_end - 1e-8:
            raise UserError(f"{cut}이 앞 구간과 겹치거나 수업 순서가 바뀌었어요. 구간과 순서를 고쳐 주세요.")
        last_end = end + source["offset"]
        segment.update(start=start, end=end, speed=speed,
                       stage=string_value(segment.get("stage", ""), f"{cut} 수업 단계"),
                       note=string_value(segment.get("note", ""), f"{cut} 메모"))
        segment.update(validate_options({key: segment[key] for key in
                                         ("teacher_cells", "teacher_zones", "masks", "blur_cells")
                                         if key in segment}, False))
        segments.append(segment)
    if not segments and not allow_empty:
        raise UserError("편집안이 비어 있어요. 남길 구간을 segments에 적어 주세요.")
    narration = []
    cut_ids = {segment["id"] for segment in segments}
    for item in list_value(data.get("narration", []), "해설"):
        voice = object_value(item, "해설").copy()
        cut = string_value(voice.get("cut"), "해설의 컷 ID")
        file = string_value(voice.get("file"), "해설 파일")
        at = finite_number(voice.get("at", 0), "해설 시작 시각")
        duck = finite_number(voice.get("duck_db", -14), "해설 중 수업 소리")
        if cut not in cut_ids or not file or at < 0 or duck > 0:
            raise UserError("해설 설정이 맞지 않아요. 컷 ID, 파일, 시작 시각과 낮출 소리 크기를 확인해 주세요.")
        narration.append({"cut": cut, "file": file, "at": at, "duck_db": duck})
    return {"version": 1, "title": title or "수업 영상", "options": options,
            "segments": segments, "narration": narration}


def conditions_for(work):
    path = work / "plan" / "조건표.json"
    values = object_value(read_json(path), "조건표")
    result = {**CONDITIONS, **values}
    for key in ("duration", "format", "edit", "anonymity"):
        result[key] = {**CONDITIONS[key], **object_value(values.get(key) or {}, key)}
    for key in ("min_sec", "max_sec", "target_sec"):
        value = result["duration"][key]
        result["duration"][key] = (finite_number(value, key) if value == 0 and key == "min_sec"
                                   else positive(value, key))
    result["max_mb"] = positive(result["max_mb"], "최대 용량")
    low, high = (result["duration"][key] for key in ("min_sec", "max_sec"))
    if low is not None and high is not None and low > high:
        raise UserError("최소 길이가 최대 길이보다 길어요. 조건표의 재생 길이를 확인해 주세요.")
    for group in ("edit", "anonymity"):
        for key in CONDITIONS[group]:
            switch(result[group][key], key, nullable=True)
    switch(result["confirmed"], "조건표 확인", nullable=True)
    switch(result["blur_faces"], "얼굴 가림", nullable=True)
    if result["captions"] not in (None, "allowed", "forbidden", "required"):
        raise UserError("자막 조건을 읽을 수 없어요. allowed, forbidden, required 중 하나를 적어 주세요.")
    spec = result["format"]
    if spec["ext"] is not None:
        spec["ext"] = string_value(spec["ext"], "확장자").lower().lstrip(".")
        if spec["ext"] not in CODECS:
            raise UserError("조건표의 확장자를 출력할 수 없어요. mp4, mov, mkv, avi, wmv 중 하나를 적어 주세요.")
    if spec["resolution"] is not None:
        spec["resolution"] = resolution(spec["resolution"])
    if spec["fps"] is not None:
        fps = fraction(spec["fps"])
        if not 0 < fps <= 240:
            raise UserError("조건표의 프레임 속도를 읽을 수 없어요. 0보다 크고 240 이하인 숫자나 분수로 적어 주세요.")
        spec["fps"] = fps.limit_denominator(100_000)
    if spec["vcodec"] is not None:
        codec = string_value(spec["vcodec"], "영상 코덱").strip().lower()
        spec["vcodec"] = V_CODEC_NAMES.get(codec, codec)
    if result["filename"] is not None:
        string_value(result["filename"], "제출 파일 이름")
    return result


def resolution(value):
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", string_value(value, "해상도"))
    if match:
        size = tuple(map(int, match.groups()))
        if min(size) >= 2 and all(side % 2 == 0 for side in size):
            return size
    raise UserError("해상도를 읽을 수 없어요. 1920x1080처럼 가로와 세로를 짝수로 적어 주세요.")


@dataclass
class Timing:
    fps: Fraction
    frames: list[int]
    fade_frames: int
    starts: list[int]

    def time(self, frames):
        return float(frames / self.fps)

    @property
    def duration(self):
        return self.time(self.starts[-1] + self.frames[-1]) if self.frames else 0.0

    @property
    def fade(self):
        return self.time(self.fade_frames)

    def lines(self):
        return [{"n": n, "start": self.time(start), "end": self.time(start + frames)}
                for n, (start, frames) in enumerate(zip(self.starts, self.frames), 1)]


def timeline(edit, fps=Fraction(30)):
    segments = edit["segments"]
    frames = [max(1, round((item["end"] - item["start"]) * fps / item["speed"])) for item in segments]
    shift = round(edit["options"]["crossfade"] * fps) if len(frames) > 1 else 0
    if shift and any(length <= shift or (0 < i < len(frames) - 1 and length < shift * 2)
                     for i, length in enumerate(frames)):
        raise UserError("이어 붙이는 시간에 비해 짧은 구간이 있어요. crossfade를 줄이거나 구간을 늘려 주세요.")
    starts = []
    cursor = 0
    for length in frames:
        starts.append(cursor)
        cursor += length - shift
    return Timing(fps, frames, shift, starts)


def settings(edit, sources, conditions, draft):
    fmt = conditions["format"]
    first = sources[edit["segments"][0]["src"]]
    default = (1080, 1920) if first["height"] > first["width"] else (1920, 1080)
    canvas = fmt["resolution"] or default
    if draft:
        canvas = fit_size(*canvas, 640, 640)
    return canvas, timeline(edit, fmt["fps"] or Fraction(30)), fmt["ext"] or "mp4"


def segment_options(edit, segment):
    options = edit["options"].copy()
    if "teacher_cells" in segment or "teacher_zones" in segment:
        options["teacher_cells"] = segment.get("teacher_cells", [])
        options["teacher_zones"] = segment.get("teacher_zones", [])
    for key in ("masks", "blur_cells"):
        if key in segment:
            options[key] = segment[key]
    return options


def validate_conditions(conditions, edit, timing, ext, draft):
    options = edit["options"]
    quicker = any(segment["speed"] > 1 for segment in edit["segments"])
    active = {"speedup": quicker, "stage_label": options["stage_label"],
              "clock_label": options["clock_label"], "narration": bool(edit["narration"])}
    names = {"speedup": "빨리 감기", "stage_label": "수업 단계 꼬리표",
             "clock_label": "수업 시각 표시", "narration": "해설"}
    problems = [f"요강에서 {names[key]} 기능을 금지해요. 편집안에서 꺼 주세요."
                for key, value in active.items() if value and conditions["edit"][key] is False]
    if conditions["captions"] == "required":
        problems.append("요강에서 자막을 요구해요. 자막을 넣을 수 있는 편집 도구를 사용해 주세요.")
    if conditions["blur_faces"] is True and not options["blur_faces"]:
        problems.append("요강에서 얼굴을 가리라고 해요. 편집안의 blur_faces를 true로 바꿔 주세요.")
    if conditions["blur_faces"] is False and options["blur_faces"]:
        problems.append("요강에서 얼굴 흐림을 금지해요. 편집안의 blur_faces를 false로 바꿔 주세요.")
    if conditions["anonymity"]["teacher"] is True and (
        not options["blur_faces"] or any(teacher_regions(segment_options(edit, item)) for item in edit["segments"])
    ):
        problems.append("교사 얼굴도 가려야 해요. 얼굴 흐림을 켜고 교사 제외 칸과 영역을 비워 주세요.")
    lower, upper = (conditions["duration"][key] for key in ("min_sec", "max_sec"))
    if lower is not None and timing.duration < lower - 1e-8:
        problems.append(f"예상 길이 {timing.duration:.2f}초가 최소 {lower:g}초보다 짧아요. 구간을 늘려 주세요.")
    if upper is not None and timing.duration > upper + 1e-8:
        problems.append(f"예상 길이 {timing.duration:.2f}초가 최대 {upper:g}초를 넘어요. 구간을 줄여 주세요.")
    if conditions["format"]["vcodec"] not in (None, CODECS[ext][0]):
        problems.append(f"{ext} 출력은 조건표의 코덱과 맞지 않아요. 확장자와 코덱을 확인해 주세요.")
    if conditions["confirmed"] is not True:
        note = "조건표 확인이 남았어요. 사람에게 확인받고 confirmed를 true로 바꿔 주세요."
        if draft:
            say(note)
        else:
            problems.append(note)
    if problems:
        raise UserError("\n".join(problems))


def reserved_filename(value):
    return bool(re.fullmatch(r"(?:CON|PRN|AUX|NUL|(?:COM|LPT)[1-9¹²³])", value.split(".")[0].upper()))


def output_name(edit, conditions, ext, draft, requested):
    value = requested or conditions["filename"]
    if value is None:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", edit["title"]).strip(" .") or "수업 영상"
        if reserved_filename(value):
            value = f"수업 영상 {value}"
    if (not value or value != value.strip(" .") or reserved_filename(value)
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)):
        raise UserError("출력 파일 이름을 쓸 수 없어요. 경로와 특수문자를 빼고 이름만 적어 주세요.")
    suffix = Path(value).suffix.lower().lstrip(".")
    if suffix in CODECS:
        if suffix != ext:
            raise UserError("출력 이름의 확장자가 조건표와 달라요. 조건표의 확장자를 써 주세요.")
        value = Path(value).stem
    return f"{value}{'_시안' if draft else ''}.{ext}"


def edit_for(work, allow_empty=False):
    sources, _ = sources_for(work)
    edit = validate_edit(read_json(work / "plan" / "편집안.json"), sources, allow_empty)
    return edit, {source["id"]: source for source in sources}


def narration_path(work, voice):
    path = local_path(work, voice["file"])
    if not path.is_file() or path.suffix.lower() != ".wav":
        raise UserError(f"{voice['file']} 해설을 읽을 수 없어요. WAV 경로를 확인해 주세요.")
    return path


def video_bitrate(max_mb, duration, audio_bps=128_000):
    bitrate = math.floor(max_mb * 1_000_000 * 8 * 0.95 / duration) - audio_bps
    if bitrate <= 0:
        raise UserError("목표 용량이 소리 크기보다 작아요. 최대 용량을 늘리거나 영상 길이를 줄여 주세요.")
    return bitrate
