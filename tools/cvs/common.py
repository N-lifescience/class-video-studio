import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIDEO_EXTS = {".mp4", ".mov", ".qt", ".m4v", ".mts", ".m2ts", ".avi", ".mkv",
              ".wmv", ".mpg", ".mpeg", ".webm", ".3gp", ".mod", ".ts", ".vob"}


class UserError(Exception):
    pass


def say(message):
    print(message, flush=True)


def seconds_text(seconds):
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def decimal(value):
    return f"{value:.9f}".rstrip("0").rstrip(".") or "0"


def natural_key(value):
    return [int(part) if part.isdecimal() else part.casefold()
            for part in re.split(r"(\d+)", str(value))]


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise UserError(f"{path.name} 파일이 없어요. 앞 단계에서 이 파일을 만들어 주세요.") from exc
    except (ValueError, UnicodeError) as exc:
        raise UserError(f"{path.name} 파일을 읽을 수 없어요. UTF-8 JSON 형식을 확인해 주세요.") from exc


@contextmanager
def pending_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cvs-", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_text(path, value):
    with pending_file(path) as temporary:
        temporary.write_text(value, encoding="utf-8", newline="\n")


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_stamp(path):
    stat = path.stat()
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns]


def local_path(work, value):
    path = work / value
    if not path.resolve().is_relative_to(work.resolve()):
        raise UserError("작업 폴더 밖을 가리키는 경로가 있어요. 작업 폴더 안의 상대 경로를 적어 주세요.")
    return path


def finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise UserError(f"{label} 값이 숫자가 아니에요. 숫자로 적어 주세요.")
    return float(value)


@contextmanager
def work_lock(work):
    cache = work / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "cvs.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UserError("이 작업 폴더에서 다른 명령이 실행 중이에요. 끝난 뒤 다시 실행해 주세요.") from exc
        yield


@contextmanager
def heartbeat(label, interval=5):
    done = threading.Event()
    start = time.monotonic()

    def report():
        while not done.wait(interval):
            say(f"{label} 작업 중이에요. {seconds_text(time.monotonic() - start)} 지났어요.")

    thread = threading.Thread(target=report, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()


def run(args, label):
    args = [str(arg) for arg in args]
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
    except FileNotFoundError as exc:
        raise UserError(f"{args[0]}을 찾을 수 없어요. doctor로 설치 방법을 확인해 주세요.") from exc
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace")
        raise UserError(f"{label} 작업을 마치지 못했어요. 파일과 설치 상태를 확인해 주세요.") from RuntimeError(error)
    return result


def ffmpeg(*args):
    return ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-filter_threads", "1",
            "-filter_complex_threads", "1", *args]


def ffprobe(path):
    result = run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                  "-of", "json", path], f"{path.name} 영상 정보 읽기")
    return json.loads(result.stdout)


@contextmanager
def raw_reader(args, label):
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen([str(arg) for arg in args], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=errors)
        except FileNotFoundError as exc:
            raise UserError("ffmpeg를 찾을 수 없어요. doctor로 설치 방법을 확인해 주세요.") from exc
        try:
            yield process.stdout
            if process.wait():
                errors.seek(0)
                detail = errors.read().decode("utf-8", errors="replace")
                raise UserError(f"{label} 영상을 읽지 못했어요. 파일을 확인해 주세요.") from RuntimeError(detail)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()


@contextmanager
def raw_writer(args, label):
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen([str(arg) for arg in args], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=errors)
        except FileNotFoundError as exc:
            raise UserError("ffmpeg를 찾을 수 없어요. doctor로 설치 방법을 확인해 주세요.") from exc
        broken = False
        try:
            try:
                yield process.stdin
                process.stdin.close()
            except BrokenPipeError:
                broken = True
            if process.wait() or broken:
                errors.seek(0)
                detail = errors.read().decode("utf-8", errors="replace")
                raise UserError(f"{label} 영상을 저장하지 못했어요. 원본과 출력 설정을 확인해 주세요.") from RuntimeError(detail)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
