import itertools
import math
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .check import save_audit
from .common import (UserError, decimal, digest, ffmpeg, ffprobe, file_stamp, heartbeat, local_path,
                     pending_file, raw_reader, raw_writer, read_json, run, say, seconds_text, write_json)
from .doctor import checked_model, korean_font
from .media import first_audio, fit_geometry, original_input
from .plan import (CODECS, conditions_for, edit_for, mask_regions, narration_path, output_name,
                   segment_options, settings, teacher_regions, validate_conditions, video_bitrate)


def draw_chip(image, text, anchor, font):
    draw = ImageDraw.Draw(image)
    w, h = image.size
    size = font.size
    pad_x, pad_y = math.ceil(size * 0.48), math.ceil(size * 0.26)
    text_box = draw.textbbox((0, 0), text, font=font)
    width = text_box[2] - text_box[0] + 2 * pad_x
    height = max(size, text_box[3] - text_box[1]) + 2 * pad_y
    x = round(w * 0.03) if anchor == "tl" else w - round(w * 0.03) - width
    y = round(h * 0.03) if anchor != "br" else h - round(h * 0.03) - height
    draw.rounded_rectangle((x, y, x + width, y + height), radius=size // 3, fill=(0, 0, 0, 180))
    text_y = y + (height - text_box[3] + text_box[1]) // 2 - text_box[1]
    draw.text((x + pad_x - text_box[0], text_y), text, font=font, fill=(255, 255, 255, 255))


def compact_label(text, font, available):
    text = " ".join(text.split())
    if font.getlength(text) <= available:
        return text
    ellipsis = "…"
    while text and font.getlength(text + ellipsis) > available:
        text = text[:-1]
    return text.rstrip() + ellipsis


def label_input(scratch, segment, source, options, canvas, timing, frame_count, typeface):
    stage = segment["stage"].strip() if options["stage_label"] else ""
    speed = segment["speed"]
    clock = options["clock_label"]
    if not (stage or speed > 1 or clock):
        return None
    font_path, font_index = typeface
    font = ImageFont.truetype(str(font_path), max(4, round(canvas[1] * 0.032)), index=font_index)
    fast = f"×{speed:g} 빨리 감기" if speed > 1 else ""
    margin = canvas[0] * 0.06 + font.size * (2.2 if fast else 1.2)
    available = max(1, canvas[0] - margin - font.getlength(fast))
    if stage:
        stage = compact_label(stage, font, available)
    start = source["offset"] + segment["start"]

    def clock_at(frame):
        return int(start + timing.time(frame) * speed + 1e-8) if clock else None

    lines = ["ffconcat version 1.0"]
    for index, (second, frames) in enumerate(itertools.groupby(range(frame_count), clock_at)):
        image = Image.new("RGBA", canvas)
        if stage:
            draw_chip(image, stage, "tl", font)
        if fast:
            draw_chip(image, fast, "tr", font)
        if second is not None:
            draw_chip(image, f"수업 {seconds_text(second)}", "br", font)
        name = f"label-{index:06}.png"
        image.save(scratch / name, compress_level=1)
        run_length = sum(1 for _ in frames)
        lines.extend([f"file {name}", "option framerate 1000",
                      f"duration {decimal(timing.time(run_length))}"])
    listing = scratch / "labels.ffconcat"
    listing.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return listing


def audio_length(path):
    info = ffprobe(path)
    audio = first_audio(info)
    values = [audio.get("duration") if audio else None, info["format"].get("duration")]
    for value in values:
        try:
            duration = float(value)
        except (ValueError, TypeError):
            continue
        if audio and math.isfinite(duration) and duration > 0:
            return duration
    raise UserError(f"{path.name} 해설의 길이를 읽을 수 없어요. WAV 파일을 다시 저장해 주세요.")


def voice_inputs(inputs, voices, duration, first_index):
    tracks = []
    for index, (voice, path) in enumerate(voices, first_index):
        available = duration - voice["at"]
        if available <= 0:
            raise UserError(f"{path.name} 해설이 컷 끝보다 늦게 시작해요. at 값을 앞당겨 주세요.")
        length = min(audio_length(path), available)
        at = round(voice["at"] * 48_000)
        length_samples = max(1, round(length * 48_000))
        fade = min(14_400, length_samples // 2)
        track = (f"[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                 f"atrim=end_sample={length_samples},asetpts=PTS-STARTPTS")
        if fade:
            track += (f",afade=t=in:ss=0:ns={fade},"
                      f"afade=t=out:ss={length_samples-fade}:ns={fade}")
        track += f",adelay={at}S:all=1[voice{index}]"
        start, end = decimal(at / 48_000), decimal((at + length_samples) / 48_000)
        fade_time = decimal(max(1, fade) / 48_000)
        gain = decimal(10 ** (voice["duck_db"] / 20))
        duck = f"1-(1-{gain})*min(clip((t-{start})/{fade_time},0,1),clip(({end}-t)/{fade_time},0,1))"
        tracks.append((track, f"[voice{index}]", f"volume='{duck}':eval=frame"))
        inputs.extend(["-i", path])
    return tracks


def segment_filters(inputs, video, source_audio, label_index, voices, next_index, duration):
    filters = [f"[0:V:0]{video}[base]"]
    if label_index is None:
        filters.append("[base]format=yuv420p[video]")
    else:
        filters.append(f"[{label_index}:v:0]setpts=PTS-0.001/TB[labels]")
        filters.append("[base][labels]overlay=eof_action=repeat,format=yuv420p[video]")
    tracks = voice_inputs(inputs, voices, duration, next_index)
    us = decimal(duration)
    audio = [f"[{source_audio}:a:0]aresample=48000:async=1:first_pts=0",
             "aformat=channel_layouts=stereo", "apad", f"atrim=duration={us}", "asetpts=PTS-STARTPTS"]
    audio.extend(duck for _, _, duck in tracks)
    filters.append(",".join(audio) + ("[class]" if tracks else "[audio]"))
    if tracks:
        filters.extend(track for track, _, _ in tracks)
        mix = "[class]" + "".join(name for _, name, _ in tracks)
        filters.append(f"{mix}amix=inputs={len(tracks)+1}:duration=first:normalize=0[audio]")
    return ";".join(filters)


def write_segment(work, target, scratch, segment, source, options, canvas, timing, frame_count,
                  voices, detector, typeface, draft):
    width, height = canvas
    x, y, image_w, image_h = fit_geometry(source, canvas)
    pad = f"pad={width}:{height}:{x}:{y}:black"
    speed = segment["speed"]
    duration = timing.time(frame_count)
    original_duration = decimal(segment["end"] - segment["start"])
    conversion = (f"trim=duration={original_duration},setpts=(PTS-STARTPTS)/{decimal(speed)},"
                  f"fps={timing.fps}:eof_action=pass,tpad=stop=-1:stop_mode=clone,trim=end_frame={frame_count}")
    original = original_input(work, source, segment["start"])
    masks = mask_regions(options)
    transformed = detector is not None or bool(masks)
    if transformed:
        # 회전과 한글 경로는 ffmpeg가 처리하고 OpenCV는 메모리의 프레임만 받아요.
        process_width = max(image_w, min(960, source["width"]))
        process_height = max(image_h, round(process_width * source["height"] / source["width"]))
        inputs = ["-f", "rawvideo", "-pix_fmt", "bgr24", "-video_size", f"{process_width}x{process_height}",
                  "-framerate", str(timing.fps), "-i", "pipe:0"]
        video = f"scale={image_w}:{image_h},setsar=1,{pad}"
    else:
        inputs = list(original)
        video = f"{conversion},scale={image_w}:{image_h},setsar=1,{pad}"
    next_index = 1
    if speed > 1 or not source["acodec"]:
        inputs += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        source_audio, next_index = 1, 2
    elif transformed:
        inputs += original
        source_audio, next_index = 1, 2
    else:
        source_audio = 0
    labels = label_input(scratch, segment, source, options, canvas, timing, frame_count, typeface)
    label_index = None
    if labels:
        inputs += ["-f", "concat", "-safe", "0", "-i", labels]
        label_index, next_index = next_index, next_index + 1
    filters = segment_filters(inputs, video, source_audio, label_index, voices, next_index, duration)
    args = ffmpeg("-v", "error", *inputs, "-filter_complex", filters,
                  "-map", "[video]", "-map", "[audio]", "-map_metadata", "-1", "-map_chapters", "-1",
                  "-t", decimal(duration), "-c:v", "libx264", "-preset", "ultrafast" if draft else "veryfast",
                  "-crf", "28" if draft else "15", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", target)
    label = f"{segment['id']} 구간 렌더"
    if not transformed:
        run(args, label)
        return

    from .faces import blur_regions, decode_frames, faces_to_blur, raw_input, tracked_frames

    size = process_width, process_height
    decode = raw_input(*original, "-map", "0:V:0",
                       "-vf", f"{conversion},scale={size[0]}:{size[1]},setsar=1",
                       "-frames:v", str(frame_count))
    zones = teacher_regions(options)
    with raw_reader(decode, label) as stream, raw_writer(args, label) as writer:
        frames = decode_frames(stream, size)
        rows = (tracked_frames(frames, detector, timing.fps, 0.5 if draft else 0.2)
                if detector else ((frame, ()) for frame in frames))
        written = 0
        for frame, boxes in rows:
            blur_regions(frame, faces_to_blur(boxes, zones), options["blur_style"], 1.6)
            blur_regions(frame, masks, options["blur_style"])
            writer.write(memoryview(frame).cast("B"))
            written += 1
        if written != frame_count:
            raise UserError(f"{segment['id']}의 프레임 수가 모자라요. 원본에서 구간의 시작과 끝을 확인해 주세요.")


def cached_segment(work, segment, source, canvas, timing, frames, options, voices, detector, typeface, draft):
    target = work / ".cache" / "seg" / f"{segment['id']}.mp4"
    metadata = target.with_suffix(".json")
    stamp = digest({
        "recipe": 3, "segment": segment, "source": source,
        "file": file_stamp(local_path(work, source["file"])), "options": options,
        "canvas": canvas, "fps": str(timing.fps), "frames": frames,
        "voices": [(voice, file_stamp(path)) for voice, path in voices],
        "font": file_stamp(typeface[0]) + [typeface[1]] if typeface else None,
        "model": file_stamp(checked_model()) if detector else None, "draft": draft,
    })
    if target.is_file() and target.stat().st_size and metadata.is_file():
        if read_json(metadata) == {"recipe": stamp, "file": file_stamp(target)}:
            say(f"{segment['id']} 구간은 저장해 둔 영상을 써요.")
            return target
    with tempfile.TemporaryDirectory(dir=work / ".cache", prefix="label-") as scratch:
        with pending_file(target) as temporary:
            write_segment(work, temporary, Path(scratch), segment, source, options, canvas, timing,
                          frames, voices, detector, typeface, draft)
    write_json(metadata, {"recipe": stamp, "file": file_stamp(target)})
    return target


def join_pair(left, right, target, timing, draft):
    a, a_frames = left
    b, b_frames = right
    fade = timing.fade_frames
    total = a_frames + b_frames - fade
    tracks = []
    for index, frames in enumerate((a_frames, b_frames)):
        tracks.extend([
            f"[{index}:v:0]settb=expr=1/({timing.fps}),setpts=N[v{index}]",
            f"[{index}:a:0]apad,atrim=duration={decimal(timing.time(frames))},asetpts=PTS-STARTPTS[a{index}]",
        ])
    if fade:
        tracks.extend([
            f"[v0][v1]xfade=transition=fade:duration={decimal(timing.fade)}:"
            f"offset={decimal(timing.time(a_frames-fade))}[video]",
            f"[a0][a1]acrossfade=ns={round(timing.fade*48000)}[audio]",
        ])
    else:
        tracks.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[video][audio]")
    run(ffmpeg("-v", "error", "-i", a, "-i", b, "-filter_complex", ";".join(tracks),
               "-map", "[video]", "-map", "[audio]", "-t", decimal(timing.time(total)),
               "-r", str(timing.fps), "-c:v", "libx264", "-preset", "ultrafast",
               "-crf", "27" if draft else "14", "-pix_fmt", "yuv420p",
               "-c:a", "flac", "-ar", "48000", "-ac", "2", target), "구간 이어 붙이기")
    return target, total


def join_all(paths, scratch, timing, draft):
    current = list(zip(paths, timing.frames))
    originals = set(paths)
    index = 0
    while len(current) > 1:
        joined = []
        for start in range(0, len(current) - 1, 2):
            index += 1
            label = f"렌더 이음매 {index}/{len(paths)-1}"
            say(f"{label}를 만들어요.")
            with heartbeat(label):
                pair = current[start:start+2]
                joined.append(join_pair(*pair, scratch / f"join-{index}.mkv", timing, draft))
            for path, _ in pair:
                if path not in originals:
                    path.unlink()
        if len(current) % 2:
            joined.append(current[-1])
        current = joined
    return current[0][0]


def encode(source, target, scratch, ext, audio_level, timing, draft, bitrate):
    _, _, vcodec, acodec = CODECS[ext]
    prefix = ffmpeg("-v", "error", "-i", source, "-map_metadata", "-1", "-map_chapters", "-1")
    video = ["-map", "0:v:0", "-c:v", vcodec, "-pix_fmt", "yuv420p", "-r", str(timing.fps)]
    if vcodec == "libx264":
        video += ["-preset", "ultrafast" if draft else "medium"]
        if bitrate is None:
            video += ["-crf", "30" if draft else "20"]
    elif bitrate is None:
        video += ["-q:v", "8" if draft else "4"]
    audio = ["-map", "0:a:0", "-af", f"loudnorm=I={decimal(audio_level)}:TP=-1.5:LRA=11,aresample=48000",
             "-c:a", acodec, "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    length = ["-t", decimal(timing.duration)]
    if bitrate is not None:
        video += ["-b:v", str(bitrate), "-passlogfile", scratch / "bitrate"]
        run([*prefix, *video, "-pass", "1", "-an", *length, "-f", "null", os.devnull], "용량 맞추기 1차 인코딩")
        video += ["-pass", "2"]
    faststart = ["-movflags", "+faststart"] if ext in {"mp4", "mov"} else []
    timestamps = ["-avoid_negative_ts", "disabled"] if ext in {"avi", "wmv"} else []
    run([*prefix, *video, *audio, *length, *faststart, *timestamps, target], "영상 내보내기")


def render(work, draft=False, output=None):
    edit, sources = edit_for(work)
    conditions = conditions_for(work)
    canvas, timing, ext = settings(edit, sources, conditions, draft)
    validate_conditions(conditions, edit, timing, ext, draft)
    destination = work / "out" / output_name(edit, conditions, ext, draft, output)
    bitrate = (video_bitrate(conditions["max_mb"], timing.duration)
               if conditions["max_mb"] and not draft else None)
    options = edit["options"]
    labels = (options["clock_label"]
              or any(item["speed"] > 1 or (options["stage_label"] and item["stage"].strip())
                     for item in edit["segments"]))
    typeface = korean_font() if labels else None
    voices = defaultdict(list)
    for voice in edit["narration"]:
        voices[voice["cut"]].append((voice, narration_path(work, voice)))
    detector = None
    if options["blur_faces"]:
        from .faces import Detector
        detector = Detector()
    paths = []
    for index, (segment, frames) in enumerate(zip(edit["segments"], timing.frames), 1):
        label = f"렌더 {index}/{len(edit['segments'])} 구간 {segment['id']}"
        say(f"{label}을 만들어요.")
        with heartbeat(label):
            paths.append(cached_segment(work, segment, sources[segment["src"]], canvas, timing,
                                        frames, segment_options(edit, segment), voices[segment["id"]],
                                        detector, typeface, draft))
    with tempfile.TemporaryDirectory(dir=work / ".cache", prefix="render-") as scratch:
        scratch = Path(scratch)
        combined = join_all(paths, scratch, timing, draft)
        say("렌더 소리 크기를 맞추고 영상을 내보내요.")
        with heartbeat("렌더 내보내기"), pending_file(destination) as temporary:
            encode(combined, temporary, scratch, ext, options["loudnorm"], timing, draft, bitrate)
            if bitrate is not None:
                limit = conditions["max_mb"] * 1_000_000
                size = temporary.stat().st_size
                if size > limit:
                    adjusted = max(1, math.floor(bitrate * limit * 0.9 / size))
                    say("렌더 파일이 최대 용량을 넘었어요. 영상 비트레이트를 낮춰 다시 만들어요.")
                    encode(combined, temporary, scratch, ext, options["loudnorm"], timing, draft, adjusted)
                if temporary.stat().st_size > limit:
                    raise UserError("출력 파일이 최대 용량에 들어가지 않아요. 해상도나 영상 길이를 줄여 주세요.")
    save_audit(work, destination, edit, sources, canvas, timing, conditions, draft)
    say(f"{destination}에 {seconds_text(timing.duration)} 영상을 저장했어요.")
