import errno
import inspect
import math
import os
import re
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlparse

from .common import (VIDEO_EXTS, UserError, decimal, ffmpeg, ffprobe, heartbeat, local_path, natural_key,
                     pending_file, read_json, run, say, seconds_text, write_json)


def video_files(directory):
    files = (path for path in directory.rglob("*")
             if path.is_file() and path.suffix.lower() in VIDEO_EXTS)
    return sorted(files, key=lambda path: (natural_key(path.relative_to(directory).as_posix()), path.as_posix()))


def place_file(source, destination, move):
    if destination.exists():
        say(f"이미 있어요. {destination.name}을 건너뛰어요.")
        return
    if move:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.replace(destination)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            with pending_file(destination) as temporary:
                shutil.copy2(source, temporary)
            source.unlink()
    else:
        with pending_file(destination) as temporary:
            shutil.copy2(source, temporary)
    say(f"{destination}에 넣었어요.")


def download_drive(work, link):
    import gdown

    with tempfile.TemporaryDirectory(dir=work / ".cache", prefix="drive-") as temporary:
        directory = Path(temporary)
        target = str(directory) + os.sep
        try:
            if "/folders/" in urlparse(link).path or urlparse(link).path.endswith("/folderview"):
                result = gdown.download_folder(url=link, output=target, quiet=True,
                                               use_cookies=False)
            else:
                # gdown 6부터는 링크를 자동으로 해석해요.
                kwargs = {"fuzzy": True} if "fuzzy" in inspect.signature(gdown.download).parameters else {}
                result = gdown.download(link, output=target, quiet=True,
                                        use_cookies=False, **kwargs)
            if not result:
                raise ValueError("empty download")
        except Exception as exc:
            raise UserError("드라이브에서 받을 수 없어요. 공유를 ‘링크가 있는 모든 사용자’로 바꾸고 링크를 확인해 주세요.") from exc
        say("링크 공유를 다시 '제한됨'으로 돌려 주세요.")
        for source in sorted(directory.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(directory)
            if source.suffix.lower() in VIDEO_EXTS:
                place_file(source, work / "원본" / relative, move=True)
            else:
                document = work / ".cache" / "받은-문서" / relative
                place_file(source, document, move=True)
                say(f"영상이 아닌 파일은 여기에서 확인해 주세요. {document}")


def fetch(work, inputs, move=False):
    for entry in inputs:
        url = urlparse(entry)
        if url.scheme in {"http", "https"}:
            if url.hostname not in {"drive.google.com", "docs.google.com"}:
                raise UserError("구글 드라이브 링크를 확인할 수 없어요. 드라이브 링크나 로컬 영상 경로를 적어 주세요.")
            with heartbeat("드라이브에서 원본 받기"):
                download_drive(work, entry)
            continue
        source = Path(entry).expanduser()
        if not source.is_file():
            raise UserError(f"{source} 파일을 찾을 수 없어요. 파일 경로를 확인해 주세요.")
        if source.suffix.lower() not in VIDEO_EXTS:
            say(f"영상이 아닌 파일은 여기에서 확인해 주세요. {source.resolve()}")
            continue
        place_file(source, work / "원본" / source.name, move)


def fraction(value, fallback=0):
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return Fraction(fallback)


def first_video(info):
    return next((stream for stream in info.get("streams", []) if stream["codec_type"] == "video"
                 and not stream.get("disposition", {}).get("attached_pic")), None)


def first_audio(info):
    return next((stream for stream in info.get("streams", []) if stream["codec_type"] == "audio"), None)


def video_duration(stream, container):
    tag = next((value for name, value in stream.get("tags", {}).items()
                if name.upper() == "DURATION"), "")
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", tag)
    tagged = sum(float(value) * unit for value, unit in zip(match.groups(), (3600, 60, 1))) if match else 0
    for value in (stream.get("duration"), tagged, container.get("duration")):
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    return 0


def source_info(path, info=None):
    if info is None:
        info = ffprobe(path)
    video, audio = first_video(info), first_audio(info)
    if not video:
        raise UserError(f"{path.name}에 영상이 없어요. 영상 파일을 넣어 주세요.")
    duration = video_duration(video, info["format"])
    if not duration:
        raise UserError(f"{path.name}의 재생 길이를 알 수 없어요. 영상 파일을 확인해 주세요.")
    rotation = next((item["rotation"] for item in video.get("side_data_list", []) if "rotation" in item),
                    video.get("tags", {}).get("rotate", 0))
    rotation = round(float(rotation or 0)) % 360
    sar = fraction(video.get("sample_aspect_ratio", "1:1").replace(":", "/"), 1) or 1
    width, height = round(video["width"] * sar), video["height"]
    if rotation in (90, 270):
        width, height = height, width
    fps = fraction(video.get("avg_frame_rate")) or fraction(video.get("r_frame_rate"))
    return {"duration": duration, "width": width, "height": height, "rotation": rotation,
            "fps": float(fps), "vcodec": video["codec_name"],
            "acodec": audio["codec_name"] if audio else None,
            "size_mb": round(path.stat().st_size / 1_000_000, 3)}


def source_id(index):
    label = ""
    while index >= 0:
        label = chr(ord("A") + index % 26) + label
        index = index // 26 - 1
    return label


def probe(work):
    sources = []
    offset = 0.0
    for index, path in enumerate(video_files(work / "원본")):
        source = {"id": source_id(index), "file": path.relative_to(work).as_posix(),
                  **source_info(path), "offset": offset}
        sources.append(source)
        offset += source["duration"]
    if not sources:
        raise UserError("원본 폴더에 영상이 없어요. fetch로 넣은 뒤 다시 실행해 주세요.")
    write_json(work / "plan" / "원본.json", {"sources": sources, "total": offset})
    say("원본  길이   표시 해상도  파일")
    for source in sources:
        say(f"{source['id']:>4}  {seconds_text(source['duration'])}  "
            f"{source['width']}×{source['height']}  {source['file']}")
    say(f"수업 영상은 모두 {seconds_text(offset)}예요.")


def sources_for(work):
    path = work / "plan" / "원본.json"
    if not path.is_file():
        raise UserError("원본 정보가 없어요. 먼저 probe를 실행해 주세요.")
    result = read_json(path)
    if not result["sources"]:
        raise UserError("원본 정보에 영상이 없어요. 원본을 넣고 probe를 다시 실행해 주세요.")
    for source in result["sources"]:
        if not local_path(work, source["file"]).is_file():
            raise UserError(f"{source['file']}을 찾을 수 없어요. 원본을 확인하고 probe를 다시 실행해 주세요.")
    return result["sources"], result["total"]


def source_at(sources, time):
    return next((source for source in reversed(sources) if source["offset"] <= time), sources[0])


def fit_size(width, height, limit_width, limit_height, allow_upscale=True):
    ratio = min(limit_width / width, limit_height / height)
    if not allow_upscale:
        ratio = min(1, ratio)
    return max(2, round(width * ratio / 2) * 2), max(2, round(height * ratio / 2) * 2)


def fit_geometry(source, canvas):
    width, height = fit_size(source["width"], source["height"], *canvas)
    return (canvas[0] - width) // 4 * 2, (canvas[1] - height) // 4 * 2, width, height


def fit_filter(source, canvas):
    x, y, width, height = fit_geometry(source, canvas)
    return f"scale={width}:{height},setsar=1,pad={canvas[0]}:{canvas[1]}:{x}:{y}:black"


def original_input(work, source, start):
    return ["-ss", decimal(start), "-i", local_path(work, source["file"])]


def measure_loudness(inputs, audio_filter=None):
    args = ["-af", "ebur128"] if audio_filter is None else [
        "-filter_complex", f"{audio_filter};[audio]ebur128[loud]", "-map", "[loud]"]
    result = run(ffmpeg("-v", "info", *inputs, "-vn", *args, "-f", "null", "-"), "소리 크기 측정")
    matches = re.findall(rb"\bI:\s*(-?\d+(?:\.\d+)?) LUFS", result.stderr)
    if not matches:
        return None
    value = float(matches[-1])
    return value if value > -70 else None
