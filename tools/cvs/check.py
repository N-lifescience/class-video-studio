import math
from dataclasses import dataclass

from .common import (VIDEO_EXTS, UserError, digest, ffprobe, file_stamp, heartbeat,
                     read_json, raw_reader, say, seconds_text, write_json, write_text)
from .media import first_audio, fit_filter, fit_geometry, measure_loudness, source_info
from .plan import (CODECS, conditions_for, edit_for, output_name, segment_options, settings,
                   teacher_regions)


@dataclass
class Result:
    name: str
    expected: str
    actual: str
    passed: bool


def range_text(low, high, unit):
    if low is None and high is None:
        return "요강에 없음"
    if low is None:
        return f"{high:g}{unit} 이하"
    if high is None:
        return f"{low:g}{unit} 이상"
    return f"{low:g}~{high:g}{unit}"


def compare(conditions, path, info, lufs, streams=()):
    fmt = conditions["format"]
    ext = path.suffix.lower().lstrip(".")
    duration = info["duration"]
    low, high = (conditions["duration"][key] for key in ("min_sec", "max_sec"))
    expected_v, expected_a = CODECS.get(fmt["ext"] or ext, (None, None, None, None))[:2]
    expected_v = fmt["vcodec"] or expected_v
    dimensions = (info["width"], info["height"])
    resolution = fmt["resolution"]
    fps = fmt["fps"]
    megabytes = path.stat().st_size / 1_000_000
    limit = conditions["max_mb"]
    rows = [
        Result("길이", range_text(low, high, "초"), f"{duration:.3f}초",
               (low is None or duration >= low - 1e-6) and (high is None or duration <= high + 1e-6)),
        Result("확장자", fmt["ext"] or "요강에 없음", ext, not fmt["ext"] or ext == fmt["ext"]),
        Result("영상 코덱", expected_v or "요강에 없음", info["vcodec"],
               not expected_v or info["vcodec"] == expected_v),
        Result("소리 코덱", expected_a or "요강에 없음", info["acodec"] or "없음",
               not expected_a or info["acodec"] == expected_a),
        Result("해상도", f"{resolution[0]}×{resolution[1]}" if resolution else "요강에 없음",
               f"{dimensions[0]}×{dimensions[1]}", not resolution or dimensions == resolution),
        Result("프레임 속도", f"{float(fps):g} fps" if fps else "요강에 없음", f"{info['fps']:g} fps",
               not fps or math.isclose(float(fps), info["fps"], rel_tol=1e-4, abs_tol=0.005)),
        Result("용량", range_text(None, limit, " MB"), f"{megabytes:.3f} MB", limit is None or megabytes <= limit),
        Result("소리 크기", "요강에 없음", f"{lufs:.1f} LUFS" if lufs is not None else "무음 또는 측정값 없음", True),
    ]
    if conditions["filename"] is not None and ext in CODECS:
        name = output_name({"title": ""}, conditions, fmt["ext"] or ext, False, None)
        rows.append(Result("파일 이름", name, path.name, path.name == name))
    captions = sum(stream["codec_type"] == "subtitle" for stream in streams)
    if conditions["captions"] in ("required", "forbidden"):
        required = conditions["captions"] == "required"
        rows.append(Result("자막 트랙", "필수" if required else "금지", f"{captions}개",
                           bool(captions) == required))
    return rows


def selected_video(work, value):
    if value is not None:
        original = value.expanduser()
        candidates = [original, work / original, work / "out" / original]
        for path in candidates:
            if path.is_file():
                return path.resolve()
        raise UserError("점검할 파일을 찾을 수 없어요. check 뒤에 영상 파일 경로를 적어 주세요.")
    files = [path for path in (work / "out").glob("*") if path.is_file()
             and not path.name.startswith(".cvs-")
             and path.suffix.lower() in VIDEO_EXTS and not is_draft(work, path)]
    if not files:
        raise UserError("제출본이 없어요. render로 만들거나 check 뒤에 영상 파일 경로를 적어 주세요.")
    return max(files, key=lambda path: (path.stat().st_mtime_ns, path.name)).resolve()


def audit_path(work, output):
    return work / ".cache" / "audit" / f"{digest(str(output.resolve()))[:24]}.json"


def saved_audit(work, path):
    metadata = audit_path(work, path)
    if metadata.is_file():
        saved = read_json(metadata)
        if saved["file"] == file_stamp(path):
            return saved
    return {}


def is_draft(work, path):
    return saved_audit(work, path).get("draft", path.stem.endswith("_시안"))


def map_teacher_spans(edit, sources, canvas, timing, conditions):
    spans = []
    for segment, line in zip(edit["segments"], timing.lines()):
        x, y, w, h = fit_geometry(sources[segment["src"]], canvas)
        cw, ch = canvas
        zones = [] if conditions["anonymity"]["teacher"] else teacher_regions(segment_options(edit, segment))
        spans.append([line["start"], line["end"],
                      [[(x + x0*w)/cw, (y + y0*h)/ch, (x + x1*w)/cw, (y + y1*h)/ch]
                       for x0, y0, x1, y1 in zones]])
    return spans


def save_audit(work, path, edit, sources, canvas, timing, conditions, draft):
    spans = map_teacher_spans(edit, sources, canvas, timing, conditions)
    write_json(audit_path(work, path), {"file": file_stamp(path), "spans": spans, "draft": draft})


def teacher_spans(work, path, info, conditions):
    if conditions["anonymity"]["teacher"]:
        return []
    saved = saved_audit(work, path)
    if saved:
        return saved["spans"]
    if not all((work / "plan" / name).is_file() for name in ("원본.json", "편집안.json")):
        return []
    try:
        edit, sources = edit_for(work)
        canvas, timing, _ = settings(edit, sources, conditions, path.stem.endswith("_시안"))
    except UserError:
        return []
    if abs(info["duration"] - timing.duration) > 0.25 or canvas != (info["width"], info["height"]):
        return []
    return map_teacher_spans(edit, sources, canvas, timing, conditions)


def outside_teacher(boxes, zones, faces_to_blur):
    # 렌더와 같은 규칙(교사 칸 안 맨 위 얼굴 하나만 교사)으로 흐렸어야 할 얼굴을 고른다.
    # 이음매에서는 두 컷이 겹쳐 보이니, 어느 한 컷의 규칙으로라도 교사면 의심하지 않는다.
    boxes = list(boxes)
    if not zones:
        return boxes
    flagged = [faces_to_blur(boxes, areas) for areas in zones]
    return [box for box in boxes if all(box in group for group in flagged)]


# 꼬리표가 놓이는 자리(왼쪽 위 단계, 오른쪽 위 빨리 감기, 오른쪽 아래 수업 시각)
LABEL_AREAS = ((0.0, 0.0, 0.45, 0.12), (0.55, 0.0, 1.0, 0.12), (0.55, 0.86, 1.0, 1.0))


def audits(work, path, info, spans):
    from .faces import (Detector, decode_frames, draw_boxes, frame_at, inspection_size,
                        faces_to_blur, picture, privacy_view, raw_input, sheet)

    directory = work / "out" / "점검"
    detector = Detector()
    size = inspection_size(info)
    fit = fit_filter(info, size)
    duration = info["duration"]
    photos = []
    with heartbeat("점검용 화면 모음 만들기"):
        for index in range(20):
            time = min(duration * (index + 0.5) / 20, max(0, duration - 1 / max(1, info["fps"])))
            frame = frame_at(path, info, time)
            # AI가 여는 그림이라 얼굴 검출과 상관없이 모자이크하고 꼬리표 자리만 남긴다.
            privacy_view(frame, LABEL_AREAS)
            photos.append((picture(frame), seconds_text(time)))
    sheet(directory / "모음.jpg", photos, rows=5)

    # 점검 시각은 초 단위예요. 얼굴 판정과 그림 가림의 문턱값을 따로 써요.
    detector.engine.setScoreThreshold(0.75)
    times, suspicious = [], []
    page = 0

    def save_page():
        nonlocal page
        page += 1
        suffix = "" if page == 1 else f"-{page:03}"
        sheet(directory / f"얼굴의심{suffix}.jpg", suspicious)
        suspicious.clear()

    args = raw_input("-i", path, "-map", "0:V:0",
                     "-vf", f"fps=1:start_time=0:round=up,{fit}")
    with heartbeat("얼굴 점검"), raw_reader(args, "얼굴 점검") as stream:
        for time, frame in enumerate(decode_frames(stream, size)):
            boxes = detector.find(frame)
            zones = [areas for start, end, areas in spans if start <= time < end]
            outside = outside_teacher(boxes, zones, faces_to_blur)
            if not outside:
                continue
            times.append(time)
            privacy_view(frame)
            draw_boxes(frame, outside, (0, 0, 255))
            suspicious.append((picture(frame), seconds_text(time)))
            if len(suspicious) == 20:
                save_page()
    if suspicious:
        save_page()
    saved_names = {"얼굴의심.jpg"} if page else set()
    saved_names.update(f"얼굴의심-{number:03}.jpg" for number in range(2, page + 1))
    for old in directory.glob("얼굴의심*.jpg"):
        if old.name not in saved_names and (old.name == "얼굴의심.jpg"
                                             or old.stem.removeprefix("얼굴의심-").isdecimal()):
            old.unlink()
    return times


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def check(work, file=None):
    conditions = conditions_for(work)
    path = selected_video(work, file)
    container = ffprobe(path)
    info = source_info(path, container)
    streams = container["streams"]
    with heartbeat("완성본 소리 크기 측정"):
        lufs = measure_loudness(["-i", path, "-map", "0:a:0"]) if first_audio(container) else None
    rows = compare(conditions, path, info, lufs, streams)
    draft = is_draft(work, path)
    # 시안은 일부러 작게, 이름 끝에 _시안을 붙여 만든다. 이 두 항목은 실패로 세지 않는다.
    skipped = {"해상도", "파일 이름"} if draft else set()
    spans = teacher_spans(work, path, info, conditions)
    times = audits(work, path, info, spans)
    faces = ", ".join(seconds_text(time) for time in times)
    rows.append(Result("얼굴 점검", "교사 제외 칸 밖에 의심 얼굴 없음" if not conditions["anonymity"]["teacher"]
                       else "의심 얼굴 없음", faces or "의심 얼굴을 찾지 못했어요.", not times))
    report = ["# 영상 점검", "", f"파일은 {path.name}이에요.", "",
              "| 항목 | 조건 | 측정 결과 | 판정 |", "| --- | --- | --- | --- |"]
    for row in rows:
        result = "통과" if row.passed else ("시안이라 넘어감" if row.name in skipped else "실패")
        report.append(f"| {cell(row.name)} | {cell(row.expected)} | {cell(row.actual)} | {result} |")
        say(f"{row.name}  {row.actual}  조건 {row.expected}  {result}")
    if conditions["anonymity"]["school"]:
        report.extend(["", "학교명이 보이는지는 모음과 영상에서 확인해 주세요."])
        say("학교명이 보이는지는 모음과 영상에서 확인해 주세요.")
    if conditions["captions"] in ("forbidden", "required"):
        report.extend(["", "영상에 그림처럼 들어간 자막은 영상에서 확인해 주세요."])
    if times:
        report.extend(["", "점검/얼굴의심.jpg에 표시한 시각을 영상에서 확인해 주세요."])
    report.append("")
    write_text(work / "out" / "점검.md", "\n".join(report))
    say("out/점검.md와 out/점검/모음.jpg에 결과를 저장했어요.")
    return int(any(not row.passed and row.name not in skipped for row in rows))
