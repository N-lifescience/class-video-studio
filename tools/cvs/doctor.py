import hashlib
import importlib
import os
import platform
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.request
from pathlib import Path

from .common import ROOT, UserError, pending_file, say

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_PATH = ROOT / "models" / MODEL_NAME
MODEL_URL = f"https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/{MODEL_NAME}"
MODEL_HASH = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
FONT_HELP = "한글 글꼴을 찾을 수 없어요. 맥의 Apple SD 산돌고딕 Neo, 윈도우의 맑은 고딕 또는 Noto Sans CJK KR을 설치해 주세요."


def backend(system=None, machine=None):
    system = system or platform.system()
    machine = machine or platform.machine()
    return "mlx_whisper" if (system == "Darwin" and machine.lower() in {"arm64", "aarch64"}) else "faster_whisper"


def korean_font():
    from PIL import ImageFont

    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [
        (Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"), 6),
        (windows / "malgunbd.ttf", 0), (windows / "malgun.ttf", 0),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"), 1),
        (Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), 1),
        (Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"), 1),
        (Path.home() / ".local/share/fonts/NotoSansCJKkr-Bold.otf", 0),
    ]
    for path, index in candidates:
        if path.is_file():
            try:
                ImageFont.truetype(str(path), 16, index=index)
            except OSError:
                continue
            return path, index
    raise UserError(FONT_HELP)


def model_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_model():
    if MODEL_PATH.is_file() and model_hash(MODEL_PATH) == MODEL_HASH:
        return MODEL_PATH
    raise UserError("얼굴 모델이 없거나 검증에 실패했어요. doctor --fetch-model을 실행해 주세요.")


def fetch_model():
    try:
        with pending_file(MODEL_PATH) as temporary:
            with urllib.request.urlopen(MODEL_URL, timeout=30) as response, temporary.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            if model_hash(temporary) != MODEL_HASH:
                raise UserError("얼굴 모델의 SHA-256이 달라요. 주소를 확인하고 다시 받아 주세요.")
    except OSError as exc:
        raise UserError("얼굴 모델을 받을 수 없어요. 인터넷에 연결한 뒤 doctor --fetch-model을 다시 실행해 주세요.") from exc


def doctor(fetch=False):
    failures = []
    if fetch:
        try:
            fetch_model()
        except UserError as exc:
            failures.append(str(exc))

    def row(name, value, trouble=None):
        # 한글은 터미널에서 두 칸을 차지하니 보이는 폭으로 줄을 맞춘다.
        width = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in name)
        say(f"{name}{' ' * max(1, 18 - width)}  {value}")
        if trouble:
            failures.append(trouble)

    version = sys.version_info
    supported = (3, 10) <= version[:2] <= (3, 13)
    row("Python", platform.python_version(), None if supported else "Python 3.10~3.13을 설치해 주세요.")
    system = platform.system()
    install = {"Darwin": "brew install ffmpeg", "Windows": "winget install Gyan.FFmpeg"}
    for name in ("ffmpeg", "ffprobe"):
        try:
            version_line = subprocess.check_output([name, "-version"], stderr=subprocess.STDOUT,
                                                   timeout=10).decode("utf-8", errors="replace").splitlines()[0]
            match = re.search(r"version\s+n?(\d+)", version_line)
            if not match or int(match[1]) < 6:
                raise ValueError(version_line)
            row(name, version_line.split()[2])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            command = install.get(system, "패키지 관리자로 ffmpeg 6 이상 설치")
            row(name, "확인 필요", f"{name} 6 이상이 필요해요. {command}를 실행해 주세요.")
    transcriber = backend()
    row("받아쓰기 백엔드", transcriber)
    for package in ("numpy", "cv2", "PIL", "gdown", "pypdf", "olefile", transcriber):
        try:
            module = importlib.import_module(package)
            if package == "cv2" and not hasattr(module, "FaceDetectorYN"):
                raise ImportError(package)
            row(package, str(getattr(module, "__version__", "쓸 수 있어요")))
        except Exception as exc:
            if package == "mlx_whisper" and "No Metal device" in str(exc):
                row(package, "Metal 접근 불가", "이 실행 환경에서 Metal을 쓸 수 없어요. 맥의 터미널에서 같은 가상 환경으로 doctor를 다시 실행해 주세요.")
            else:
                row(package, "불러오기 실패", f"{package}를 불러올 수 없어요. python -m pip install -r requirements.txt를 실행해 주세요.")
    actual = model_hash(MODEL_PATH) if MODEL_PATH.is_file() else None
    row("얼굴 모델 SHA-256", actual or "파일 없음",
        None if actual == MODEL_HASH else f"얼굴 모델을 확인해 주세요. doctor --fetch-model로 받거나 이 주소에서 받아 models/에 넣어 주세요. {MODEL_URL}")
    try:
        path, index = korean_font()
        row("한글 글꼴", f"{path} ({index})")
    except (UserError, ImportError):
        row("한글 글꼴", "확인 필요", FONT_HELP)
    for failure in failures:
        say(f"- {failure}")
    return int(bool(failures))
