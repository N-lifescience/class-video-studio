import struct
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

from .common import UserError, natural_key, say, write_text

HWP_HELP = "HWP 문서의 글을 읽을 수 없어요. 한글에서 PDF로 저장해 주세요."
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def hwp_paragraph(data):
    parts = []
    index = 0
    start = 0
    while index + 1 < len(data):
        code = int.from_bytes(data[index:index + 2], "little")
        if code >= 32:
            index += 2
            continue
        parts.append(data[start:index].decode("utf-16-le", errors="replace"))
        if code in (10, 13):
            parts.append("\n")
        elif code == 9:
            parts.append("\t")
        index += 16 if 1 <= code <= 23 and code not in (10, 13) else 2
        start = index
    parts.append(data[start:index].decode("utf-16-le", errors="replace"))
    return "".join(parts)


def section_paragraphs(data):
    offset = 0
    while offset + 4 <= len(data):
        value, = struct.unpack_from("<I", data, offset)
        offset += 4
        tag, size = value & 0x3FF, value >> 20
        if size == 0xFFF:
            size, = struct.unpack_from("<I", data, offset)
            offset += 4
        if offset + size > len(data):
            raise ValueError("truncated HWP record")
        if tag == 67:
            yield hwp_paragraph(data[offset:offset + size])
        offset += size


def read_hwp(path):
    import olefile

    try:
        with olefile.OleFileIO(path) as archive:
            header = archive.openstream("FileHeader").read()
            if not header.startswith(b"HWP Document File") or len(header) < 40:
                raise ValueError("invalid HWP header")
            flags, = struct.unpack_from("<I", header, 36)
            if flags & (2 | 4 | 16):
                raise UserError(HWP_HELP)
            sections = sorted((parts for parts in archive.listdir() if len(parts) == 2
                               and parts[0] == "BodyText" and parts[1].startswith("Section")),
                              key=lambda parts: natural_key(parts[1]))
            if not sections:
                raise UserError(HWP_HELP)
            paragraphs = []
            for section in sections:
                data = archive.openstream(section).read()
                if flags & 1:
                    data = zlib.decompress(data, -15)
                paragraphs.extend(section_paragraphs(data))
            return "".join(paragraphs)
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        raise UserError(HWP_HELP) from exc


def xml_paragraphs(document, ns):
    root = ElementTree.fromstring(document)
    lines = []
    for paragraph in root.iter(ns + "p"):
        line = []
        # 표 속 문단은 별도로 읽어요.
        stack = list(reversed(paragraph))
        while stack:
            element = stack.pop()
            if element.tag == ns + "p":
                continue
            if element.tag == ns + "t":
                line.append(element.text or "")
            elif element.tag in {ns + "br", ns + "lineBreak"}:
                line.append("\n")
            elif element.tag == ns + "tab":
                line.append("\t")
            stack.extend(reversed(element))
        if line:
            lines.append("".join(line))
    if not lines:
        lines = [node.text or "" for node in root.iter(ns + "t")]
    return "\n".join(lines)


def read_document(path):
    suffix = path.suffix.lower()
    if suffix == ".hwp":
        return read_hwp(path)
    if suffix in {".hwpx", ".docx"}:
        try:
            with zipfile.ZipFile(path) as archive:
                if suffix == ".docx":
                    return xml_paragraphs(archive.read("word/document.xml"), WORD)
                sections = sorted((name for name in archive.namelist()
                                   if name.startswith("Contents/section") and name.endswith(".xml")),
                                  key=natural_key)
                if not sections:
                    raise ValueError("missing HWPX sections")
                return "\n".join(xml_paragraphs(archive.read(name), HP) for name in sections)
        except (zipfile.BadZipFile, RuntimeError, KeyError, ValueError, ElementTree.ParseError) as exc:
            raise UserError(f"{path.name}의 글을 읽을 수 없어요. 문서 편집기에서 PDF로 저장해 주세요.") from exc
    if suffix == ".pdf":
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError

        try:
            reader = PdfReader(path)
            if reader.is_encrypted and not reader.decrypt(""):
                raise UserError("PDF가 잠겨 있어요. 암호를 풀어 다시 저장해 주세요.")
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except PyPdfError as exc:
            raise UserError("PDF의 글을 읽을 수 없어요. 문서 편집기에서 다시 저장해 주세요.") from exc
        if not text.strip():
            raise UserError("PDF에 복사할 글자가 없어요. 문자 인식으로 글을 만든 뒤 다시 실행해 주세요.")
        return text
    if suffix in {".txt", ".md"}:
        data = path.read_bytes()
        encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp949")
        for encoding in encodings:
            try:
                return data.decode(encoding)
            except UnicodeError:
                continue
        raise UserError("텍스트를 읽을 수 없어요. UTF-8로 다시 저장해 주세요.")
    raise UserError("이 문서 형식을 읽을 수 없어요. PDF, 한글, Word 또는 TXT로 저장해 주세요.")


def readdoc(path, output=None):
    text = read_document(path)
    if output:
        write_text(Path(output), text)
        say(f"{output}에 글을 저장했어요.")
    else:
        say(text)
