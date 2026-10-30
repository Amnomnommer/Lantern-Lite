"""Text from a PDF the owner chose, with no third-party library (owner, 2026-10-23).

Most of the owner's files are PDFs. This reads ordinary text PDFs: it walks the
page tree, inflates content streams (FlateDecode), follows compressed object
streams, decodes text with each font's ToUnicode map (or the standard
single-byte encoding), and puts spaces and line breaks where the text moves.
Scanned PDFs (pictures of pages) hold no text, so they yield nothing, and the
caller says so instead of guessing. The phone's `PhonePdf.kt` is an exact port
(shared `pdf-vectors-v1.json`). Content is untrusted data: nothing in a PDF is run.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field

MAX_PDF_BYTES = 30_000_000
MAX_TEXT_CHARS = 5_000_000
_OBJ = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_REF = rb"(\d+)\s+(\d+)\s+R"
_WS = b" \t\r\n\f\x00"
_DELIM = b"()<>[]{}/%"
# Windows-1252 differs from Latin-1 in 0x80-0x9F; the common punctuation there is mapped.
_WIN = {0x80: "€", 0x82: "‚", 0x84: "„", 0x85: "…", 0x86: "†", 0x87: "‡", 0x89: "‰", 0x8B: "‹", 0x8C: "Œ",
        0x91: "‘", 0x92: "’", 0x93: "“", 0x94: "”", 0x95: "•", 0x96: "–", 0x97: "—", 0x99: "™", 0x9B: "›",
        0x9C: "œ", 0xA0: " "}


class PdfError(ValueError):
    """The file is not a PDF this reader can open."""


@dataclass
class _Font:
    cmap: dict[bytes, str] = field(default_factory=dict)
    code_bytes: int = 1


def _objects(data: bytes) -> dict[int, bytes]:
    """Every `N G obj ... endobj` body by object number, including those inside object streams."""
    objects: dict[int, bytes] = {}
    for match in _OBJ.finditer(data):
        start = match.end()
        end = data.find(b"endobj", start)
        if end < 0:
            continue
        objects[int(match.group(1))] = data[start:end]
    for number, body in list(objects.items()):
        if re.search(rb"/Type\s*/ObjStm\b", body.split(b"stream", 1)[0]):
            stream = _stream(body)
            n = re.search(rb"/N\s+(\d+)", body)
            first = re.search(rb"/First\s+(\d+)", body)
            if stream is None or n is None or first is None:
                continue
            header = stream[: int(first.group(1))].split()
            offsets = [(int(header[i]), int(header[i + 1])) for i in range(0, min(len(header), 2 * int(n.group(1))) - 1, 2)]
            for index, (inner, offset) in enumerate(offsets):
                begin = int(first.group(1)) + offset
                stop = int(first.group(1)) + offsets[index + 1][1] if index + 1 < len(offsets) else len(stream)
                objects.setdefault(inner, stream[begin:stop])
    return objects


def _stream(body: bytes) -> bytes | None:
    """An object's stream, inflated when it is FlateDecode; None when there is none or the filter is unknown."""
    at = re.search(rb"stream\r?\n", body)
    if at is None:
        return None
    raw = body[at.end():]
    end = raw.rfind(b"endstream")
    raw = raw[:end] if end >= 0 else raw
    head = body[: at.start()]
    filters = re.findall(rb"/(\w+Decode)\b", head)
    if not filters:
        return raw.rstrip(b"\r\n")
    if filters != [b"FlateDecode"]:
        return None
    try:
        return zlib.decompressobj().decompress(raw)
    except zlib.error:
        return None


def _resolve(objects: dict[int, bytes], value: bytes) -> bytes:
    match = re.fullmatch(_REF, value.strip())
    return objects.get(int(match.group(1)), b"") if match else value


def _dict_value(body: bytes, key: bytes) -> bytes | None:
    """The raw value after `/key` in a dictionary: a reference, a nested << >> dict, an array or a token."""
    match = re.search(rb"/" + key + rb"(?![A-Za-z0-9])\s*", body)
    if match is None:
        return None
    rest = body[match.end():]
    ref = re.match(_REF, rest)
    if ref:
        return ref.group(0)
    if rest.startswith(b"<<"):
        depth, i = 0, 0
        while i < len(rest) - 1:
            if rest[i:i + 2] == b"<<":
                depth, i = depth + 1, i + 2
                continue
            if rest[i:i + 2] == b">>":
                depth, i = depth - 1, i + 2
                if depth == 0:
                    return rest[:i]
                continue
            i += 1
        return rest
    if rest.startswith(b"["):
        end = rest.find(b"]")
        return rest[: end + 1] if end >= 0 else rest
    token = re.match(rb"[^\s/<>\[\]()]+", rest)
    return token.group(0) if token else None


def _cmap(text: bytes) -> _Font:
    """A ToUnicode CMap: bfchar and bfrange entries, and the code width from the codespace."""
    font = _Font()
    space = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", text)
    if space:
        font.code_bytes = max(1, len(space.group(1)) // 2)

    def unicode(hex_text: bytes) -> str:
        raw = bytes.fromhex(hex_text.decode("ascii"))
        return raw.decode("utf-16-be", errors="replace") if len(raw) % 2 == 0 else raw.decode("latin-1")

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.DOTALL):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            if len(src) % 2 == 0 and len(dst) % 2 == 0:  # malformed codes are skipped, never guessed
                font.cmap[bytes.fromhex(src.decode("ascii"))] = unicode(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.DOTALL):
        for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block):
            low, high = int(lo, 16), int(hi, 16)
            width = len(lo) // 2
            if high - low > 65535 or len(lo) % 2 or high >= 256 ** width or (dst.startswith(b"<") and len(dst) % 2):
                continue
            if dst.startswith(b"["):
                targets = re.findall(rb"<([0-9A-Fa-f]*)>", dst)
                for offset, target in enumerate(targets[: high - low + 1]):
                    if len(target) % 2 == 0:
                        font.cmap[(low + offset).to_bytes(width, "big")] = unicode(target)
            else:
                base = bytes.fromhex(dst[1:-1].decode("ascii"))
                for offset in range(high - low + 1):
                    shifted = base[:-1] + bytes([(base[-1] + offset) & 0xFF]) if base else b""
                    font.cmap[(low + offset).to_bytes(width, "big")] = (
                        shifted.decode("utf-16-be", errors="replace") if len(shifted) % 2 == 0 else shifted.decode("latin-1"))
    return font


def _fonts(objects: dict[int, bytes], resources: bytes) -> dict[bytes, _Font]:
    fonts: dict[bytes, _Font] = {}
    table = _dict_value(resources, b"Font")
    if table is None:
        return fonts
    table = _resolve(objects, table)
    for match in re.finditer(rb"/([^\s/<>\[\]()]+)\s*" + _REF, table):
        body = objects.get(int(match.group(2)), b"")
        font = _Font()
        to_unicode = _dict_value(body, b"ToUnicode")
        if to_unicode is not None:
            stream = _stream(_resolve(objects, to_unicode))
            if stream:
                font = _cmap(stream)
        elif re.search(rb"/Subtype\s*/Type0\b", body):
            font.code_bytes = 2
        fonts[match.group(1)] = font
    return fonts


def _decode(raw: bytes, font: _Font | None) -> str:
    if font is not None and font.cmap:
        out, i, width = [], 0, font.code_bytes
        while i < len(raw):
            code = raw[i:i + width]
            out.append(font.cmap.get(code, ""))
            i += width
        return "".join(out)
    if font is not None and font.code_bytes == 2:
        return ""  # a two-byte font without a Unicode map cannot be read honestly
    return "".join(_WIN.get(b, chr(b)) for b in raw)


def _tokens(content: bytes):
    """PDF content tokens: ("str", bytes), ("num", float), ("name", bytes), ("op", bytes), "[" and "]"."""
    i, n = 0, len(content)
    while i < n:
        c = content[i]
        if c in _WS:
            i += 1
        elif c == 0x25:  # % comment
            while i < n and content[i] not in b"\r\n":
                i += 1
        elif c == 0x28:  # ( literal string )
            depth, i, out = 1, i + 1, bytearray()
            while i < n and depth:
                ch = content[i]
                if ch == 0x5C and i + 1 < n:  # backslash
                    nxt = content[i + 1]
                    escapes = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12, 0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}
                    if nxt in escapes:
                        out.append(escapes[nxt])
                        i += 2
                    elif 0x30 <= nxt <= 0x37:
                        digits = re.match(rb"[0-7]{1,3}", content[i + 1:i + 4]).group(0)
                        out.append(int(digits, 8) & 0xFF)
                        i += 1 + len(digits)
                    elif nxt in b"\r\n":
                        i += 2 if content[i + 1:i + 3] != b"\r\n" else 3
                    else:
                        out.append(nxt)
                        i += 2
                    continue
                if ch == 0x28:
                    depth += 1
                elif ch == 0x29:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                out.append(ch)
                i += 1
            yield ("str", bytes(out))
        elif c == 0x3C and content[i + 1:i + 2] != b"<":  # <hex string>
            end = content.find(b">", i)
            end = n if end < 0 else end
            digits = re.sub(rb"[^0-9A-Fa-f]", b"", content[i + 1:end])
            if len(digits) % 2:
                digits += b"0"
            yield ("str", bytes.fromhex(digits.decode("ascii")))
            i = end + 1
        elif content[i:i + 2] in (b"<<", b">>"):
            i += 2
        elif c in b"[]":
            yield (chr(c), None)
            i += 1
        elif c == 0x2F:  # /name
            j = i + 1
            while j < n and content[j] not in _WS and content[j] not in _DELIM:
                j += 1
            yield ("name", content[i + 1:j])
            i = j
        else:
            j = i
            while j < n and content[j] not in _WS and content[j] not in _DELIM:
                j += 1
            if j == i:
                i += 1
                continue
            word = content[i:j]
            yield ("num", float(word)) if _NUMBER.fullmatch(word) else ("op", word)
            i = j


def _page_text(content: bytes, fonts: dict[bytes, _Font]) -> str:
    out: list[str] = []
    stack: list[tuple[str, object]] = []
    font: _Font | None = None
    size = 12.0
    run = 0  # characters since the last positioning, to tell a word gap from a kerning step
    in_array, array = False, []

    def emit(text: str) -> None:
        nonlocal run
        if text:
            out.append(text)
            run += len(text)

    def gap(kind: str) -> None:
        if out and not out[-1].endswith(("\n", " ")):
            out.append(kind)
        elif out and kind == "\n" and out[-1].endswith(" "):
            out[-1] = out[-1].rstrip(" ") + "\n"

    for kind, value in _tokens(content):
        if kind == "[":
            in_array, array = True, []
            continue
        if kind == "]":
            in_array = False
            stack.append(("arr", array))
            continue
        if in_array:
            array.append((kind, value))
            continue
        if kind != "op":
            stack.append((kind, value))
            continue
        op = value
        if op == b"Tf":
            names = [v for k, v in stack if k == "name"]
            numbers = [v for k, v in stack if k == "num"]
            font = fonts.get(names[-1]) if names else None
            size = abs(numbers[-1]) if numbers and numbers[-1] else size
        elif op == b"Tj" or op in (b"'", b'"'):
            if op != b"Tj":
                gap("\n")
            strings = [v for k, v in stack if k == "str"]
            if strings:
                emit(_decode(strings[-1], font))
        elif op == b"TJ":
            arrays = [v for k, v in stack if k == "arr"]
            for k, v in arrays[-1] if arrays else []:
                if k == "str":
                    emit(_decode(v, font))
                elif k == "num" and v < -200:
                    gap(" ")
        elif op in (b"Td", b"TD"):
            numbers = [v for k, v in stack if k == "num"]
            if len(numbers) >= 2 and abs(numbers[-1]) > 0.01:
                gap("\n")
            elif len(numbers) >= 2 and numbers[-2] > (run * 0.5 + 0.2) * size:
                gap(" ")  # moved clearly further than the text it follows: a word gap, not kerning
            run = 0
        elif op in (b"T*", b"ET", b"Tm"):
            gap("\n")
            run = 0
        stack = []
    return "".join(out)


def _pages(objects: dict[int, bytes]) -> list[tuple[bytes, bytes]]:
    """(page dictionary, inherited resources) in reading order, through the page tree."""
    catalog = next((body for body in objects.values() if re.search(rb"/Type\s*/Catalog\b", body)), None)
    root_ref = _dict_value(catalog, b"Pages") if catalog else None
    pages: list[tuple[bytes, bytes]] = []
    seen: set[int] = set()

    def walk(ref: bytes, inherited: bytes) -> None:
        match = re.fullmatch(_REF, ref.strip())
        if match is None or int(match.group(1)) in seen or len(pages) > 10_000:
            return
        seen.add(int(match.group(1)))
        body = objects.get(int(match.group(1)), b"")
        resources = _dict_value(body, b"Resources")
        resources = _resolve(objects, resources) if resources is not None else inherited
        if re.search(rb"/Type\s*/Pages\b", body):
            kids = _dict_value(body, b"Kids") or b""
            for kid in re.finditer(_REF, kids):
                walk(kid.group(0), resources)
        else:
            pages.append((body, resources))

    if root_ref is not None:
        walk(root_ref, b"")
    if not pages:  # no usable tree: every page in object order
        pages = [(body, _resolve(objects, _dict_value(body, b"Resources") or b""))
                 for _, body in sorted(objects.items()) if re.search(rb"/Type\s*/Page\b", body)]
    return pages


def _flow(lines: list[str]) -> str:
    """Join a page's lines into paragraphs: a line much shorter than the page's longest (a heading or
    a paragraph's last line) ends its paragraph; words hyphenated across a line break are mended."""
    if not lines:
        return ""
    widest = max(len(line) for line in lines)
    paragraphs, text = [], ""
    for line in lines:
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line
        else:
            text = f"{text} {line}" if text else line
        if len(line) < 0.8 * widest:
            paragraphs.append(text)
            text = ""
    if text:
        paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_text(data: bytes) -> str:
    """The readable text of a PDF, pages separated by blank lines; empty when it holds no text (a scan)."""
    if not data.startswith(b"%PDF"):
        raise PdfError("this is not a PDF file")
    if len(data) > MAX_PDF_BYTES:
        raise PdfError(f"the PDF is larger than {MAX_PDF_BYTES // 1_000_000} MB")
    objects = _objects(data)
    if re.search(rb"/Encrypt\s*" + _REF, data):
        raise PdfError("the PDF is password-protected")
    texts: list[str] = []
    total = 0
    for page, resources in _pages(objects):
        fonts = _fonts(objects, resources)
        contents = _dict_value(page, b"Contents")
        refs = re.findall(_REF, contents) if contents else []
        content = b"".join((_stream(objects.get(int(number), b"")) or b"") + b"\n" for number, _ in refs)
        text = _page_text(content, fonts)
        page_text = _flow([" ".join(line.split()) for line in text.split("\n") if line.strip()])
        if page_text:
            texts.append(page_text)
            total += len(page_text)
            if total > MAX_TEXT_CHARS:
                break
    return "\n\n".join(texts)[:MAX_TEXT_CHARS]
