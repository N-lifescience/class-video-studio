"""수업 시각(예: 14:17-15:34)을 편집안 구간으로 바꾼다.

AI가 원본마다 offset 을 빼고 문장 경계를 찾는 계산을 손으로 하면 틀리기 쉬워서 만든 명령이다.
받아쓴 문장 경계에 맞춰 시작은 0.5초 앞, 끝은 0.7초 뒤로 잡고, 그 안의 받아쓴 글을 함께 보여 준다.
"""
import json
import re

from .common import UserError, read_json, say, seconds_text
from .media import sources_for

LEAD, TAIL = 0.5, 0.7


def clock(value):
    parts = value.strip().split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        numbers = []
    if not 1 <= len(numbers) <= 3 or any(number < 0 for number in numbers):
        raise UserError(f"「{value}」 시각을 읽을 수 없어요. 14:17 이나 1:02:30 처럼 적어 주세요.")
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds


def parse_range(value):
    match = re.fullmatch(r"\s*([\d:.]+)\s*[-–~]\s*([\d:.]+)\s*", value)
    if not match:
        raise UserError(f"「{value}」 구간을 읽을 수 없어요. 14:17-15:34 처럼 적어 주세요.")
    start, end = clock(match[1]), clock(match[2])
    if end <= start:
        raise UserError(f"「{value}」 구간의 끝이 시작보다 빨라요. 시작-끝 순서로 적어 주세요.")
    return start, end


def snap(lines, start, end):
    """수업 시각 [start, end] 를 문장 경계에 맞춘다. 걸친 문장은 통째로 넣는다."""
    inside = [line for line in lines if line["end"] > start and line["start"] < end]
    if not inside:
        return start, end, []
    return inside[0]["start"] - LEAD, inside[-1]["end"] + TAIL, inside


def span(work, value, cut=None, stage=""):
    sources, _ = sources_for(work)
    start, end = parse_range(value)
    path = work / "plan" / "받아쓰기.json"
    lines = read_json(path) if path.is_file() else []
    if not lines:
        say("받아쓰기가 없어서 문장 경계에 맞추지 않고 그대로 바꿔요.")
    snapped_start, snapped_end, inside = snap(lines, start, end)
    owner = next((source for source in reversed(sources) if source["offset"] <= start + 1e-6), sources[0])
    begin = owner["offset"]
    finish = begin + owner["duration"]
    if end > finish + 0.5:
        raise UserError(f"{value} 구간이 원본 {owner['id']}({seconds_text(begin)}–{seconds_text(finish)})의 끝을 넘어요. "
                        "원본 경계에서 두 구간으로 나눠 주세요.")
    local_start = max(0.0, snapped_start - begin)
    local_end = min(owner["duration"], snapped_end - begin)
    segment = {"id": cut or "c??", "src": owner["id"], "start": round(local_start, 2), "end": round(local_end, 2),
               "stage": stage, "note": "", "speed": 1}
    say(f"수업 {seconds_text(begin + local_start)}–{seconds_text(begin + local_end)} · "
        f"원본 {owner['id']} · {local_end - local_start:.1f}초")
    for line in inside:
        say(f"  [{seconds_text(line['start'])}] {line['text']}")
    print(json.dumps(segment, ensure_ascii=False))
