import math
import re
import unicodedata
from collections import defaultdict
from itertools import groupby

from .analyze import overlap, silences_in
from .common import (UserError, digest, ffmpeg, file_stamp, heartbeat, local_path, pending_file,
                     run, say, seconds_text, write_json, write_text)
from .doctor import backend
from .media import sources_for

MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
FILLERS = (
    "시청해주셔서감사합니다", "시청해주셔서고맙습니다", "봐주셔서감사합니다",
    "끝까지시청해주셔서감사합니다", "구독과좋아요부탁드립니다", "구독과좋아요",
    "다음영상에서만나요", "다음영상에서뵙겠습니다", "자막제공",
)


def normalized(text):
    value = unicodedata.normalize("NFKC", text).casefold()
    return "".join(letter for letter in value if letter.isalnum())


def filter_hallucinations(rows, quiet=None):
    quiet = quiet or {}
    kept = []
    for key, group in groupby(rows, key=lambda row: normalized(row["text"])):
        lines = list(group)
        if not key:
            continue
        for row in lines[:1] if len(lines) >= 3 else lines:
            start, end = row["t0"], row["t1"]
            if (any(key.startswith(filler) for filler in FILLERS)
                    and overlap(quiet.get(row["src"], []), start, end) >= (end - start) * 0.7):
                continue
            kept.append(row)
    return kept, len(rows) - len(kept)


def transcriber(model):
    kind = backend()
    if kind == "mlx_whisper":
        import mlx_whisper

        def transcribe(path):
            result = mlx_whisper.transcribe(str(path), path_or_hf_repo=model or MLX_MODEL,
                                            language="ko", word_timestamps=False, verbose=None)
            return ((item["start"], item["end"], item["text"]) for item in result["segments"])
        return transcribe

    import ctranslate2
    from faster_whisper import WhisperModel

    device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"

    def load(device):
        return WhisperModel(model or ("large-v3-turbo" if device == "cuda" else "small"),
                            device=device, compute_type="auto")

    engine = None

    def transcribe(path):
        nonlocal engine, device
        try:
            if engine is None:
                engine = load(device)
            segments, _ = engine.transcribe(str(path), language="ko", vad_filter=True)
            return [(segment.start, segment.end, segment.text) for segment in segments]
        except RuntimeError:
            if device != "cuda":
                raise
            say("GPU에서 받아쓰기를 시작할 수 없어요. CPU로 이어서 받아쓸게요.")
            device = "cpu"
            engine = None
            return transcribe(path)
    return transcribe


def wav_for(work, source):
    path = local_path(work, source["file"])
    key = digest(file_stamp(path))[:16]
    wav = work / ".cache" / "wav" / f"{source['id']}-{key}.wav"
    if not wav.is_file():
        with pending_file(wav) as temporary:
            run(ffmpeg("-v", "error", "-i", path, "-map", "0:a:0", "-vn",
                       "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", temporary), "받아쓰기용 소리 추출")
    return wav


def transcribe(work, model=None, force=False):
    output = work / "plan" / "받아쓰기.json"
    transcript = output.with_suffix(".txt")
    if output.is_file() and transcript.is_file() and not force:
        say("받아쓰기 결과가 이미 있어요. 다시 받아쓰려면 --force를 붙여 주세요.")
        return
    sources, _ = sources_for(work)
    runner = None
    rows = []
    quiet = defaultdict(list)
    for index, source in enumerate(sources, 1):
        if not source["acodec"]:
            say(f"받아쓰기 {index}/{len(sources)} 원본 {source['id']}에 소리가 없어요.")
            continue
        label = f"받아쓰기 {index}/{len(sources)} 원본 {source['id']}"
        say(f"{label}을 시작해요.")
        with heartbeat(label):
            wav = wav_for(work, source)
            duration = source["duration"]
            quiet[source["id"]] = silences_in(wav, duration, minimum=0.5)
            if overlap(quiet[source["id"]], 0, duration) >= duration - 0.05:
                continue
            try:
                if runner is None:
                    runner = transcriber(model)
                for start, end, text in runner(wav):
                    if not math.isfinite(start) or not math.isfinite(end):
                        continue
                    start, end = max(0, start), min(duration, end)
                    if start < end:
                        offset = source["offset"]
                        rows.append({"src": source["id"], "t0": start, "t1": end,
                                     "start": start + offset, "end": end + offset,
                                     "text": re.sub(r"\s+", " ", text).strip()})
            except Exception as exc:
                if backend() == "mlx_whisper" and "No Metal device" in str(exc):
                    raise UserError("이 실행 환경에서 Metal을 쓸 수 없어요. 맥의 터미널에서 받아쓰기를 실행해 주세요.") from exc
                raise UserError("받아쓰기 모델을 실행할 수 없어요. doctor로 설치 상태를 확인하고 모델을 처음 쓸 때는 인터넷에 연결해 주세요.") from exc
    rows.sort(key=lambda row: row["start"])
    kept, removed = filter_hallucinations(rows, quiet)
    write_json(output, kept)
    write_text(transcript, "".join(f"[{seconds_text(row['start'])}] {row['text']}\n" for row in kept))
    say(f"받아썼어요. 반복되거나 무음에서 생긴 {removed}줄을 걸렀어요.")
