import math
import re
from pathlib import Path

from .common import (decimal, ffmpeg, heartbeat, local_path, read_json, run, say,
                     seconds_text, write_json)
from .media import measure_loudness, sources_for

STAMP = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def silences_in(path, duration, minimum=4):
    args = ffmpeg("-v", "info", "-i", path, "-map", "0:a:0", "-vn", "-t", decimal(duration),
                  "-af", f"silencedetect=noise=-35dB:d={minimum}", "-f", "null", "-")
    result = run(args, "조용한 구간 찾기")
    events = re.findall(rf"silence_(start|end):\s*({STAMP})", result.stderr.decode(errors="replace"))
    times = []
    start = None
    for kind, time in events:
        time = max(0, min(float(time), duration))
        if kind == "start":
            start = time
        elif start is not None:
            times.append((start, time))
            start = None
    if start is not None:
        times.append((start, duration))
    return [(start, end) for start, end in times if end > start]


def scenes_in(path, duration):
    filters = "scale=320:320:force_original_aspect_ratio=decrease:force_divisible_by=2,select='gt(scene,0.3)',showinfo"
    result = run(ffmpeg("-v", "info", "-i", path, "-map", "0:V:0", "-an", "-t", decimal(duration),
                        "-vf", filters, "-fps_mode", "vfr", "-f", "null", "-"), "장면 바뀜 찾기")
    return [float(value) for value in re.findall(rf"\bpts_time:\s*({STAMP})",
                                                result.stderr.decode(errors="replace"))
            if 0 <= float(value) < duration]


def merge_ranges(intervals):
    result = []
    for low, high in sorted(intervals):
        if high <= low:
            continue
        if result and low <= result[-1][1] + 1e-6:
            result[-1][1] = max(high, result[-1][1])
        else:
            result.append([low, high])
    return result


def overlap(intervals, start, end):
    return sum(max(0, min(end, high) - max(start, low)) for low, high in intervals)


def windows(total, step):
    return ((start, min(start + step, total)) for start in range(0, math.ceil(total), step))


def loudness_window(work, sources, start, end):
    inputs = []
    branches = []
    for source in sources:
        offset = source["offset"]
        low, high = max(start, offset), min(end, offset + source["duration"])
        if low >= high:
            continue
        duration = decimal(high - low)
        if source["acodec"]:
            inputs.extend(["-ss", decimal(low - offset), "-t", duration,
                           "-i", local_path(work, source["file"])])
        else:
            inputs.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
        i = len(branches)
        branches.append(f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                        f"apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a{i}]")
    if not branches:
        return None
    labels = "".join(f"[a{i}]" for i in range(len(branches)))
    filters = ";".join([*branches, f"{labels}concat=n={len(branches)}:v=0:a=1[audio]"])
    return measure_loudness(inputs, filters)


def analyze(work):
    sources, total = sources_for(work)
    silences = []
    scenes = []
    for index, source in enumerate(sources, 1):
        label = f"분석 {index}/{len(sources)} 원본"
        say(f"{label}을 살펴봐요.")
        path = local_path(work, source["file"])
        offset, duration = source["offset"], source["duration"]
        with heartbeat(label):
            quiet = silences_in(path, duration) if source["acodec"] else [(0, duration)]
            silences.extend((start + offset, end + offset) for start, end in quiet)
            scenes.extend(time + offset for time in scenes_in(path, duration))
    silences = merge_ranges(silences)
    transcript = work / "plan" / "받아쓰기.json"
    speech = merge_ranges((row["start"], row["end"]) for row in read_json(transcript)) if transcript.is_file() else None
    loudness, speech_ratio = [], []
    with heartbeat("30초 칸 소리 크기 측정"):
        for start, end in windows(total, 30):
            lufs = loudness_window(work, sources, start, end)
            loudness.append({"start": start, "end": end, "lufs": lufs})
            if speech is not None:
                speech_ratio.append({"start": start, "end": end, "ratio": round(overlap(speech, start, end) / (end - start), 4)})
    result = {"silences": silences, "scene_changes": scenes, "loudness": loudness,
              "speech_ratio": speech_ratio}
    write_json(work / "plan" / "분석.json", result)
    for start, end in windows(total, 300):
        quiet = overlap(silences, start, end)
        changes = sum(start <= time < end for time in scenes)
        levels = [row["lufs"] for row in loudness if start <= row["start"] < end and row["lufs"] is not None]
        level = f"{10 * math.log10(sum(10 ** (value / 10) for value in levels) / len(levels)):.1f} LUFS" if levels else "측정값 없음"
        talked = f", 말소리 {overlap(speech, start, end) / (end - start):.0%}" if speech is not None else ""
        say(f"{seconds_text(start)}–{seconds_text(end)}  조용한 시간 {quiet:.0f}초, 장면 바뀜 {changes}번, 소리 {level}{talked}")
    say("plan/분석.json에 저장했어요.")
