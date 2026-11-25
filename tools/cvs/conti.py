from fractions import Fraction

from .common import (decimal, ffmpeg, file_stamp, heartbeat, local_path, pending_file,
                     read_json, run, say, seconds_text, write_json)
from .media import fit_size, original_input
from .plan import conditions_for, edit_for, timeline

CONTI_PATH = "conti/V1.json"
CONTI_URL = "https://n-lifescience.github.io/conti-studio/"


def preview_audio(speed):
    filters = []
    while speed > 2:
        filters.append("atempo=2")
        speed /= 2
    if speed != 1:
        filters.append(f"atempo={decimal(speed)}")
    return [*filters, "aresample=48000", "apad"]


def preview(work, segment, source):
    clip = work / "media" / "clips" / f"{segment['id']}.mp4"
    metadata = clip.with_suffix(".json")
    local = local_path(work, source["file"])
    speed, start, end = (segment[key] for key in ("speed", "start", "end"))
    key = {"version": 1, "src": segment["src"], "start": start, "end": end, "speed": speed,
           "source_file": file_stamp(local)}
    if clip.is_file() and clip.stat().st_size and metadata.is_file():
        if read_json(metadata) == {**key, "clip": file_stamp(clip)}:
            return
    frames = max(1, round((end - start) / speed * 30))
    duration = decimal(frames / 30)
    width, height = fit_size(source["width"], source["height"], 640, 640)
    inargs = original_input(work, source, start)
    if not source["acodec"]:
        inargs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    video = (f"trim=duration={decimal(end-start)},setpts=(PTS-STARTPTS)/{decimal(speed)},fps=30:eof_action=pass,"
             f"tpad=stop=-1:stop_mode=clone,trim=end_frame={frames},scale={width}:{height},setsar=1")
    audio = ",".join(preview_audio(speed))
    with pending_file(clip) as temporary:
        run(ffmpeg("-v", "error", *inargs, "-map", "0:V:0",
                   "-map", "0:a:0" if source["acodec"] else "1:a:0", "-map_metadata", "-1",
                   "-vf", video, "-af", audio, "-t", duration,
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", temporary),
            f"{segment['id']} 콘티 미리보기")
    write_json(metadata, {**key, "clip": file_stamp(clip)})


def span(segment, source):
    offset = source["offset"]
    return f"{seconds_text(offset + segment['start'])}–{seconds_text(offset + segment['end'])}"


def clip_element(source, index, tall):
    # 원본 방향 그대로 화면 안에 들어가게 둔다(세로 원본이 가로 콘티에 섞여도 잘리지 않게).
    canvas = (1080, 1920) if tall else (1920, 1080)
    ratio = source["width"] / source["height"]
    height = round(min(canvas[1], canvas[0] / ratio))
    return {"t": "vid", "src": "", "pos": [canvas[0] // 2, canvas[1] // 2], "h": height,
            "aspect": f"{source['width']} / {source['height']}", "from": 0, "at": index}


def conti(work, reset_notes=False, resolved=()):
    edit, sources = edit_for(work)
    conditions = conditions_for(work)
    timing = timeline(edit, conditions["format"]["fps"] or Fraction(30))
    segments = edit["segments"]
    first = sources[segments[0]["src"]]
    tall = first["height"] > first["width"]
    path = work / CONTI_PATH
    previous = read_json(path) if path.is_file() and not reset_notes else {}
    # 반영한 컷(--resolved)은 메모와 상태를 비워 다음 conti-notes 에 다시 나오지 않게 한다.
    saved = {cut["id"]: cut for cut in previous.get("cuts", []) if cut["id"] not in set(resolved)}
    script, cuts = [], []
    previous_stage = None
    for index, item in enumerate(segments, 1):
        source = sources[item["src"]]
        cut, stage, note, speed = (item[key] for key in ("id", "stage", "note", "speed"))
        label = f"콘티 {index}/{len(segments)} 구간 {cut}"
        say(f"{label}을 준비해요.")
        with heartbeat(label):
            preview(work, item, source)
        description = f"[{stage}] {span(item, source)}" if stage else span(item, source)
        if note:
            description += f" · {note}"
        if speed > 1:
            description += f" · {speed:g}배속"
        line = {"n": index, "display": description, "role": "narrator", "section": stage}
        if stage != previous_stage:
            line["sectionTitle"] = stage
            previous_stage = stage
        script.append(line)
        old = saved.get(cut, {})
        element = clip_element(source, index, tall)
        element["src"] = f"clips/{cut}.mp4"
        cuts.append({
            "id": cut, "from": index, "kind": "black",
            "els": [element],
            "plan": note, "memo": old.get("memo", ""), "status": old.get("status", ""),
        })
    target = conditions["duration"]["target_sec"] or timing.duration
    # paths 를 꼭 적는다. 비어 있으면 콘티 작업실이 예전 꼴(video/public)로 찾아 클립과 시각을 못 읽는다.
    document = {"conti": 1, "id": "V1", "title": edit["title"], "shape": "tall" if tall else "wide",
                "promise": f"수업발표대회 제출본 — 목표 {seconds_text(target)}",
                "paths": {"script": "script/{id}.json", "timeline": "conti/{id}-timeline.json",
                          "public": "media", "stills": "stills/{id}"},
                "cuts": cuts}
    write_json(work / "script" / "V1.json", script)
    write_json(work / "conti" / "V1-timeline.json",
               {"source": "수업 영상 편집실", "duration": timing.duration, "lines": timing.lines()})
    write_json(path, document)
    say(f"예상 총길이는 {seconds_text(timing.duration)}예요.")
    say(f"콘티 작업실({CONTI_URL})에서 이 작업 폴더를 여세요.")


def conti_notes(work):
    edit, sources = edit_for(work, allow_empty=True)
    cuts = {cut["id"]: cut for cut in read_json(work / CONTI_PATH)["cuts"]}
    notes = []
    for segment in edit["segments"]:
        cut_id, stage = segment["id"], segment["stage"]
        source = sources[segment["src"]]
        cut = cuts.get(cut_id)
        memo = cut.get("memo", "") if cut else ""
        status = cut.get("status", "") if cut else ""
        if cut and status != "fix" and not memo:
            continue
        message = memo or ("콘티에서 빠졌어요. 구간을 뺄지 확인해 주세요." if cut is None
                           else "고쳐 달라고 표시했어요. 고칠 곳을 확인해 주세요.")
        say(f"{cut_id} [{stage} {span(segment, source)}] 고칠 점: {message}")
        notes.append({"id": cut_id, "stage": stage, "src": segment["src"],
                      "start": segment["start"] + source["offset"], "end": segment["end"] + source["offset"],
                      "memo": memo, "status": status, "missing": cut is None})
    write_json(work / "plan" / "콘티메모.json", notes)
    say(f"확인할 컷 {len(notes)}개를 plan/콘티메모.json에 저장했어요.")
