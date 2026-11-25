import math
from collections import deque

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .common import (UserError, decimal, ffmpeg, heartbeat, local_path, pending_file,
                     run, say, seconds_text)
from .doctor import checked_model
from .media import fit_filter, fit_size, source_at, sources_for


# 교실 영상 65장으로 견줘 보니 960폭·0.6 은 한 화면에 3.6개, 원본 폭·0.45 는 4.6개쯤 찾았다.
# 흐림은 놓치는 쪽이 더 위험하니 기준을 낮추고 원본 폭(최대 1920)에서 찾는다.
DETECT_WIDTH = 1920
SCORE = 0.45


class Detector:
    def __init__(self, score=SCORE):
        if hasattr(cv2, "setLogLevel"):
            cv2.setLogLevel(2)
        else:
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        try:
            model = np.frombuffer(checked_model().read_bytes(), dtype=np.uint8)
            self.engine = cv2.FaceDetectorYN.create("onnx", model, np.empty(0, np.uint8),
                                                    (DETECT_WIDTH, 1080), score, 0.3, 5000)
        except cv2.error as exc:
            raise UserError("얼굴 모델을 열 수 없어요. doctor로 OpenCV와 모델을 확인해 주세요.") from exc

    def find(self, frame):
        height, width = frame.shape[:2]
        target = min(width, DETECT_WIDTH)
        size = (target, max(2, round(height * target / width)))
        scaled = frame if size == (width, height) else cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
        self.engine.setInputSize(size)
        try:
            _, faces = self.engine.detect(scaled)
        except cv2.error as exc:
            raise UserError("영상에서 얼굴을 찾는 중 오류가 났어요. doctor로 모델을 확인해 주세요.") from exc
        if faces is None:
            return []
        return [(float(x / size[0]), float(y / size[1]), float((x + w) / size[0]), float((y + h) / size[1]))
                for x, y, w, h, *_ in faces if w > 0 and h > 0]


def center(box):
    x0, y0, x1, y1 = box
    return (x0 + x1) / 2, (y0 + y1) / 2


def is_teacher(box, zones):
    x, y = center(box)
    return any(left <= x < right and top <= y < bottom for left, top, right, bottom in zones)


def faces_to_blur(boxes, zones):
    """흐릴 얼굴. 교사 칸 안에서는 맨 위 얼굴 하나(서 있는 교사)만 남기고 나머지는 흐린다.

    칸은 화면의 1/9이라 넓어서 교사 옆에 앉은 학생도 칸에 든다. 교사는 서 있고 학생은 앉아 있으니
    칸 안에서 가장 위에 있는 얼굴을 교사로 본다. 같은 얼굴이 이어 잡기로 두 상자가 된 경우도 남긴다.
    """
    boxes = list(boxes)
    inside = [box for box in boxes if is_teacher(box, zones)]
    if not inside:
        return boxes
    top = min(inside, key=lambda box: box[1])
    # 같은 얼굴의 다른 상자(앞뒤 프레임에서 이어 잡은 것) 범위: 맨 위 얼굴 상자를 1.5배로
    tx, ty = center(top)
    dx, dy = (top[2] - top[0]) * 0.75, (top[3] - top[1]) * 0.75

    def same_face(box):
        x, y = center(box)
        return abs(x - tx) <= dx and abs(y - ty) <= dy

    return [box for box in boxes if not (is_teacher(box, zones) and same_face(box))]


def box_pixels(box, width, height, expansion=1):
    x, y = center(box)
    dx, dy = (box[2] - box[0]) * expansion / 2, (box[3] - box[1]) * expansion / 2
    return (max(0, math.floor((x - dx) * width)), max(0, math.floor((y - dy) * height)),
            min(width, math.ceil((x + dx) * width)), min(height, math.ceil((y + dy) * height)))


def blur_regions(frame, boxes, style="blur", expansion=1):
    height, width = frame.shape[:2]
    for box in boxes:
        left, top, right, bottom = box_pixels(box, width, height, expansion)
        if left >= right or top >= bottom:
            continue
        region = frame[top:bottom, left:right]
        small = cv2.resize(region, (min(6, right - left), min(6, bottom - top)),
                           interpolation=cv2.INTER_AREA)
        if style == "blur":
            small = cv2.GaussianBlur(small, (0, 0), 1.8)
        interpolate = cv2.INTER_LINEAR if style == "blur" else cv2.INTER_NEAREST
        region[:] = cv2.resize(small, (right - left, bottom - top), interpolation=interpolate)
    return frame


def match_boxes(before, after):
    candidates = []
    for i, old in enumerate(before):
        ox, oy = center(old)
        for j, new in enumerate(after):
            nx, ny = center(new)
            diameter_x = max(old[2] - old[0], new[2] - new[0])
            diameter_y = max(old[3] - old[1], new[3] - new[1])
            distance = math.hypot((nx - ox) / diameter_x, (ny - oy) / diameter_y)
            if distance <= 1:
                candidates.append((distance, i, j))
    pairs = {}
    used = set()
    for _, i, j in sorted(candidates):
        if i not in pairs and j not in used:
            pairs[i] = j
            used.add(j)
    return pairs


def tracked_frames(frames, detector, fps, sample_seconds):
    # 다음 검출까지 프레임을 잠깐 보관하고 앞뒤 상자 사이를 이어요.
    stride = max(1, round(float(fps) * sample_seconds))
    hold = round(float(fps) * 1.0)
    iterator = iter(frames)
    first = next(iterator, None)
    if first is None:
        return
    queue = [first]
    before = detector.find(first)
    trailing = deque()
    index = 0
    while queue:
        while len(queue) < stride and (frame := next(iterator, None)) is not None:
            queue.append(frame)
        next_frame = next(iterator, None)
        after = detector.find(next_frame) if next_frame is not None else []
        pairs = match_boxes(before, after)
        arriving = [box for j, box in enumerate(after) if j not in pairs.values()]
        trailing.extend((index + hold, box) for i, box in enumerate(before) if i not in pairs)
        for offset, frame in enumerate(queue):
            while trailing and trailing[0][0] < index + offset:
                trailing.popleft()
            boxes = [box for _, box in trailing] + arriving
            ratio = offset / len(queue)
            for i, j in pairs.items():
                boxes.append(tuple(a + (b - a) * ratio for a, b in zip(before[i], after[j])))
            yield frame, boxes
        index += len(queue)
        before = after
        queue = [next_frame] if next_frame is not None else []


def decode_frames(stream, size):
    width, height = size
    count = width * height * 3
    while pixels := stream.read(count):
        if len(pixels) != count:
            raise UserError("영상 프레임이 중간에 끊겼어요. 원본 파일을 확인해 주세요.")
        yield np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3).copy()


def raw_input(*args):
    return ffmpeg("-v", "error", *args, "-an", "-sn", "-dn", "-c:v", "rawvideo",
                  "-pix_fmt", "bgr24", "-threads:v", "1", "-f", "rawvideo", "pipe:1")


def inspection_size(source):
    return fit_size(source["width"], source["height"], 960, 1920, allow_upscale=False)


def frame_at(path, source, time):
    size = inspection_size(source)
    args = raw_input("-ss", decimal(time), "-i", path, "-map", "0:V:0",
                     "-vf", fit_filter(source, size), "-frames:v", "1")
    result = run(args, "확인용 화면 추출")
    if len(result.stdout) != size[0] * size[1] * 3:
        raise UserError("확인용 화면을 읽을 수 없어요. 원본과 probe 결과를 확인해 주세요.")
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape(size[1], size[0], 3).copy()


def privacy_view(frame, keep=()):
    # AI가 여는 점검 그림용: 얼굴 검출에 기대지 않고 화면 전체를 굵게 모자이크한다.
    # keep(0~1 상자)은 꼬리표처럼 또렷해야 하는 자리만 되살린다.
    height, width = frame.shape[:2]
    small = cv2.resize(frame, (max(1, width // 16), max(1, height // 16)), interpolation=cv2.INTER_AREA)
    coarse = cv2.resize(small, (width, height), interpolation=cv2.INTER_NEAREST)
    for box in keep:
        left, top, right, bottom = box_pixels(box, width, height)
        coarse[top:bottom, left:right] = frame[top:bottom, left:right]
    frame[:] = coarse
    return frame


def draw_boxes(frame, boxes, color):
    height, width = frame.shape[:2]
    for box in boxes:
        x0, y0, x1, y1 = box_pixels(box, width, height)
        cv2.rectangle(frame, (x0, y0), (x1 - 1, y1 - 1), color, 2)


def picture(frame):
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def grid_overlay(frame):
    height, width = frame.shape[:2]
    for i in (1, 2):
        cv2.line(frame, (width * i // 3, 0), (width * i // 3, height), (255, 255, 255), 1)
        cv2.line(frame, (0, height * i // 3), (width, height * i // 3), (255, 255, 255), 1)
    size = max(0.55, min(width, height) / 500)
    for column in range(3):
        for row in range(3):
            x, y = width * column // 3 + 8, height * row // 3 + 25
            label = f"{'ABC'[column]}{row+1}"
            for color, thickness in (((0, 0, 0), 4), ((255, 255, 255), 1)):
                cv2.putText(frame, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, size, color, thickness, cv2.LINE_AA)


def sheet(path, photos, columns=4, rows=None):
    thumb_w, thumb_h, caption_h = 320, 220, 22
    rows = rows or math.ceil(len(photos) / columns)
    output = Image.new("RGB", (thumb_w * columns, (thumb_h + caption_h) * rows), "#14171c")
    draw = ImageDraw.Draw(output)
    for index, (image, caption) in enumerate(photos):
        x, y = (index % columns) * thumb_w, (index // columns) * (thumb_h + caption_h)
        image.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        output.paste(image, (x + (thumb_w - image.width) // 2, y + (thumb_h - image.height) // 2))
        draw.text((x + 8, y + thumb_h + 4), caption, fill="white")
    with pending_file(path) as temporary:
        output.save(temporary, quality=90)


def faces_grid(work, every):
    sources, total = sources_for(work)
    if not math.isfinite(every) or every <= 0 or every < total / 1e12:
        raise UserError("화면을 뽑을 간격이 너무 짧거나 올바르지 않아요. --every에 초를 적어 주세요.")
    count = math.ceil(total / every)
    take = min(count, 12)
    indices = [round(index * (count - 1) / (take - 1)) for index in range(take)] if take > 1 else [0]
    detector = Detector()
    photos = []
    with heartbeat("얼굴 확인 화면 만들기"):
        for index in indices:
            time = index * every
            source = source_at(sources, time)
            local = min(time - source["offset"], max(0, source["duration"] - 1 / max(1, source["fps"])))
            frame = frame_at(local_path(work, source["file"]), source, local)
            boxes = detector.find(frame)
            privacy_view(frame)
            draw_boxes(frame, boxes, (50, 255, 255))
            grid_overlay(frame)
            photos.append((picture(frame), f"{seconds_text(time)}  |  {len(boxes)}"))
    sheet(work / "plan" / "얼굴확인.jpg", photos)
    say(f"모자이크한 화면 {len(photos)}장을 plan/얼굴확인.jpg에 저장했어요. 노란 상자는 찾은 얼굴이에요.")


def draw_regions(frame, regions, color):
    height, width = frame.shape[:2]
    for box in regions:
        x0, y0, x1, y1 = box_pixels(box, width, height)
        cv2.rectangle(frame, (x0 + 2, y0 + 2), (x1 - 3, y1 - 3), color, 3)


def plan_grid(work):
    """편집안 구간마다 가운데 화면 한 장: 교사 칸(초록 테두리), 늘 흐릴 칸(회색), 얼굴(노랑=흐림, 초록=교사)."""
    from .plan import edit_for, mask_regions, segment_options, teacher_regions

    edit, sources = edit_for(work)
    segments = edit["segments"]
    take = min(len(segments), 12)
    picks = [segments[round(i * (len(segments) - 1) / (take - 1))] for i in range(take)] if take > 1 else segments
    detector = Detector()
    photos = []
    with heartbeat("구간별 얼굴 확인 화면 만들기"):
        for segment in picks:
            source = sources[segment["src"]]
            middle = (segment["start"] + segment["end"]) / 2
            frame = frame_at(local_path(work, source["file"]), source, middle)
            options = segment_options(edit, segment)
            zones = teacher_regions(options)
            boxes = detector.find(frame)
            blurred = faces_to_blur(boxes, zones)
            privacy_view(frame)
            draw_regions(frame, zones, (60, 200, 60))
            draw_regions(frame, mask_regions(options), (160, 160, 160))
            draw_boxes(frame, blurred, (50, 255, 255))
            draw_boxes(frame, [box for box in boxes if box not in blurred], (60, 200, 60))
            grid_overlay(frame)
            photos.append((picture(frame), f"{segment['id']} {seconds_text(source['offset'] + middle)}  |  {len(boxes)}"))
    sheet(work / "plan" / "얼굴확인-구간.jpg", photos)
    say(f"구간 {len(photos)}개의 가운데 화면을 plan/얼굴확인-구간.jpg에 저장했어요. "
        "초록 테두리는 교사 칸, 초록 상자는 교사로 본 얼굴, 노란 상자는 흐릴 얼굴이에요.")
