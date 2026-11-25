import argparse
import sys
from pathlib import Path

from cvs.common import UserError, say, work_lock


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, add_help=False)
        self.add_argument("-h", "--help", action="help", help="사용법을 보여 줘요.")

    def format_help(self):
        return (super().format_help().replace("usage:", "사용법:", 1)
                .replace("positional arguments:", "인자:").replace("optional arguments:", "옵션:")
                .replace("options:", "옵션:"))

    def format_usage(self):
        return super().format_usage().replace("usage:", "사용법:", 1)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "명령을 읽을 수 없어요. --help에서 명령과 옵션을 확인해 주세요.\n")


def parser():
    root = Parser(prog="cvs.py", description="수업 영상을 자르고 제출 조건에 맞춰요.")
    root.add_argument("--debug", action="store_true", help="오류의 자세한 내용을 보여 줘요.")
    commands = root.add_subparsers(dest="command", required=True, metavar="명령")
    entries = {
        "doctor": "도구와 모델을 준비했는지 확인해요.",
        "init": "작업 폴더와 빈 조건표, 편집안을 만들어요.",
        "fetch": "원본 영상을 작업 폴더에 넣어요.",
        "probe": "원본의 길이와 해상도를 읽어요.",
        "readdoc": "요강이나 지도안을 글로 읽어요.",
        "transcribe": "수업에서 한 말을 받아써요.",
        "analyze": "조용한 구간과 장면이 바뀐 시각을 찾아요.",
        "faces-grid": "모자이크한 화면에 찾은 얼굴과 교사 위치를 고를 격자를 그려요.",
        "span": "수업 시각(14:17-15:34)을 편집안 구간으로 바꾸고 그 안의 말을 보여 줘요.",
        "conti": "콘티 파일과 컷마다 볼 미리보기를 만들어요.",
        "conti-notes": "콘티 작업실의 메모를 읽어요.",
        "render": "편집안을 영상으로 만들어요.",
        "check": "출력 영상을 조건표와 대조해요.",
    }
    for name, description in entries.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                             help="오류의 자세한 내용을 보여 줘요.")
        if name not in {"doctor", "readdoc"}:
            command.add_argument("work", metavar="작업", type=Path, help="작업 폴더의 경로예요.")
        if name == "doctor":
            command.add_argument("--fetch-model", action="store_true", help="얼굴 모델을 내려받아 확인해요.")
        if name == "fetch":
            command.add_argument("inputs", nargs="+", metavar="링크 또는 파일", help="원본을 가져올 곳이에요.")
            command.add_argument("--move", action="store_true", help="로컬 영상을 복사하는 대신 옮겨요.")
        if name == "readdoc":
            command.add_argument("file", type=Path, metavar="파일", help="글을 읽을 문서예요.")
            command.add_argument("--out", type=Path, metavar="파일", help="읽은 글을 파일로 저장해요.")
        if name == "transcribe":
            command.add_argument("--model", metavar="모델", help="사용할 받아쓰기 모델이에요.")
            command.add_argument("--force", action="store_true", help="결과가 있어도 다시 받아써요.")
        if name == "faces-grid":
            command.add_argument("--every", type=float, default=60, metavar="초", help="화면을 뽑는 간격이에요.")
            command.add_argument("--plan", action="store_true",
                                 help="편집안 구간마다 한 장씩 뽑아 교사 칸이 맞는지 봐요.")
        if name == "span":
            command.add_argument("range", metavar="구간", help="수업 시각 구간이에요. 예: 14:17-15:34")
            command.add_argument("--id", metavar="컷", help="편집안에 쓸 컷 이름이에요. 예: c03")
            command.add_argument("--stage", default="", metavar="단계", help="수업 단계 이름이에요. 예: 전개 1")
        if name == "conti":
            command.add_argument("--reset-notes", action="store_true", help="기존 메모와 상태를 비워요.")
            command.add_argument("--resolved", default="", metavar="컷",
                                 help="반영한 컷의 메모와 상태를 비워요. 예: c03,c05")
        if name == "render":
            command.add_argument("--draft", action="store_true", help="작은 시안 영상으로 만들어요.")
            command.add_argument("--out", metavar="이름", help="출력할 파일 이름이에요.")
        if name == "check":
            command.add_argument("file", nargs="?", type=Path, metavar="파일", help="따로 점검할 영상이에요.")
    return root


def dispatch(args, work):
    if args.command == "init":
        from cvs.plan import init
        return init(work)
    if args.command == "fetch":
        from cvs.media import fetch
        return fetch(work, args.inputs, args.move)
    if args.command == "probe":
        from cvs.media import probe
        return probe(work)
    if args.command == "transcribe":
        from cvs.transcribe import transcribe
        return transcribe(work, args.model, args.force)
    if args.command == "analyze":
        from cvs.analyze import analyze
        return analyze(work)
    if args.command == "faces-grid":
        from cvs.faces import faces_grid, plan_grid
        return plan_grid(work) if args.plan else faces_grid(work, args.every)
    if args.command == "span":
        from cvs.span import span
        return span(work, args.range, args.id, args.stage)
    if args.command in {"conti", "conti-notes"}:
        from cvs.conti import conti, conti_notes
        if args.command == "conti-notes":
            return conti_notes(work)
        resolved = {item.strip() for item in args.resolved.split(",") if item.strip()}
        return conti(work, args.reset_notes, resolved)
    if args.command == "render":
        from cvs.render import render
        return render(work, args.draft, args.out)
    from cvs.check import check
    return check(work, args.file)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from cvs.doctor import doctor
            return doctor(args.fetch_model)
        if args.command == "readdoc":
            from cvs.docs import readdoc
            readdoc(args.file.expanduser(), args.out.expanduser() if args.out else None)
            return 0
        work = args.work.expanduser().resolve()
        if args.command not in {"init", "fetch"} and not work.is_dir():
            raise UserError("작업 폴더가 없어요. 폴더 경로를 확인하거나 init을 실행해 주세요.")
        with work_lock(work):
            return dispatch(args, work) or 0
    except KeyboardInterrupt:
        say("작업을 멈췄어요. 이어서 하려면 같은 명령을 다시 실행해 주세요.")
        return 130
    except Exception as exc:
        if args.debug:
            raise
        if isinstance(exc, UserError):
            say(str(exc))
        elif isinstance(exc, ImportError):
            say("필요한 패키지를 불러올 수 없어요. doctor로 설치 상태를 확인해 주세요.")
        elif isinstance(exc, OSError):
            say("파일을 읽거나 저장할 수 없어요. 경로, 권한과 디스크 공간을 확인해 주세요.")
        else:
            say("작업을 마치지 못했어요. 입력 파일을 확인하고 자세한 오류는 --debug로 확인해 주세요.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
