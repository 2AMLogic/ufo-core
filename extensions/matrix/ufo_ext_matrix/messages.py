"""The messages the surface sends: Markdown as a rich client renders it, what a message relates to,
and how one reply becomes several events.

`events.py` reads what a room said; this writes what the bot says back. A reply carries the words in
`body` and the same words as HTML in `formatted_body`, so a homeserver holds each reply twice over —
more where escaping expands a character into an entity. An event may weigh `EVENT_LIMIT_BYTES` in
all, envelope included, so a reply is written in parts of at most `PART_BUDGET_BYTES` of Markdown —
a fence opener of its own length excepted, which one part keeps intact — a sixteenth of the event
budget, which leaves the widest expansion of those bytes room to fit.

Nothing here imports `ufo` or an HTTP client, so the contract tests hold these rules on a checkout
with neither installed."""

import html
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ufo_ext_matrix.events import REPLACE_RELATION, TEXT_MSGTYPE, THREAD_RELATION

HTML_FORMAT = "org.matrix.custom.html"
EDIT_PREFIX = "* "
EVENT_LIMIT_BYTES = 65536
PART_BUDGET_BYTES = 4096
FENCE = "```"
FILE_MSGTYPE = "m.file"
MEDIA_MSGTYPES = {"image": "m.image", "video": "m.video", "audio": "m.audio"}
LINK_SCHEMES = frozenset({"http", "https", "mailto", "matrix"})
LINE_BREAK = "<br />"
HEADING = re.compile(r"(#{1,6})\s+(.*)")
BULLET = re.compile(r"[-*+]\s+(.*)")
NUMBERED = re.compile(r"\d+[.)]\s+(.*)")
QUOTE = re.compile(r">\s?(.*)")
INLINE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\[(?P<text>[^\]\n]*)\]\((?P<href>[^)\s]+)\)"
    r"|\*\*(?P<strong>[^\n]+?)\*\*"
    r"|(?<!\w)\*(?P<italic>[^*\n]+)\*(?!\w)"
    r"|(?<!\w)_(?P<under>[^_\n]+)_(?!\w)"
)


def reply_relation(event_id: str, thread_root: str | None) -> dict[str, Any]:
    """What a message the bot sends relates to. A reply to a message sent to the room itself is a
    rich reply to that message; one answering a message in a thread hangs under the thread's root,
    and names the message it answers as the reply a client without threads falls back to."""
    replied = {"m.in_reply_to": {"event_id": event_id}}
    if thread_root is None:
        return replied
    return {
        "rel_type": THREAD_RELATION,
        "event_id": thread_root,
        "is_falling_back": True,
        **replied,
    }


def msgtype_for(media_type: str) -> str:
    """Which message a file is sent as. A room renders a picture, a clip, and a recording itself;
    anything else is a file to download."""
    return MEDIA_MSGTYPES.get(media_type.partition("/")[0].strip().lower(), FILE_MSGTYPE)


def message_content(
    text: str, msgtype: str, relates_to: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """One message the bot sends: the words, the same words as HTML for a client that renders it,
    and what the message relates to where it answers something."""
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": text,
        "format": HTML_FORMAT,
        "formatted_body": html_body(text),
    }
    if relates_to:
        content["m.relates_to"] = dict(relates_to)
    return content


def edit_content(text: str, event_id: str) -> dict[str, Any]:
    """One message rewritten in place: the new words as `m.new_content`, and the same words marked
    as an edit in the fallback a client that shows no edits renders instead."""
    written = message_content(text, TEXT_MSGTYPE)
    return {
        **written,
        "body": EDIT_PREFIX + written["body"],
        "m.new_content": written,
        "m.relates_to": {"rel_type": REPLACE_RELATION, "event_id": event_id},
    }


def file_content(
    msgtype: str,
    filename: str,
    caption: str | None,
    media_type: str,
    size_bytes: int,
    url: str,
    relates_to: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One uploaded file as the message that carries it. A file the turn gave words to reads as
    those words with the download name beside them; one that got none reads as its name."""
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": caption or filename,
        "url": url,
        "info": {"mimetype": media_type, "size": size_bytes},
    }
    if caption:
        content["filename"] = filename
    if relates_to:
        content["m.relates_to"] = dict(relates_to)
    return content


def encrypted_file_content(
    msgtype: str,
    filename: str,
    caption: str | None,
    media_type: str,
    size_bytes: int,
    sealed: Mapping[str, Any],
    relates_to: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One sealed file as the message that carries it.

    The `mxc://` moves inside `file`, beside the key that opens it, so the event names no url a
    client could fetch without the key. `info.mimetype` stays the file's own type rather than the
    ciphertext's: it describes what the bytes become, and a room renders from it after
    decrypting."""
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": caption or filename,
        "file": dict(sealed),
        "info": {"mimetype": media_type, "size": size_bytes},
    }
    if caption:
        content["filename"] = filename
    if relates_to:
        content["m.relates_to"] = dict(relates_to)
    return content


def parts(markdown: str, budget: int = PART_BUDGET_BYTES) -> tuple[str, ...]:
    """One reply as the messages it is sent in: whole paragraphs, in the order they were written,
    each at most `budget` bytes — except a fenced block whose opener alone reaches the budget,
    which stays intact on one part, itself still far under the event limit. A paragraph of its own
    length is cut at a line boundary, and a fenced block cut that way is closed and opened again,
    so no part leaves a fence standing open and every part renders on its own."""
    written: list[str] = []
    for block in _blocks(markdown):
        written.extend(_bounded(block, budget))
    packed = _packed(written, budget)
    return tuple(packed) if packed else (markdown,)


def html_body(markdown: str) -> str:
    """The `formatted_body` a rich client renders: paragraphs, headings, lists, quotes, fenced
    code, and inline emphasis, code, and links. Everything else is the words themselves, escaped —
    the subset Matrix names and no tag a client would strip. A link is written only under a scheme
    a room may follow, and anything else keeps its words and loses its target."""
    lines = markdown.splitlines()
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
        elif line.startswith(FENCE):
            index, block = _fenced(lines, index)
            rendered.append(block)
        elif (heading := HEADING.fullmatch(line)) is not None:
            level = len(heading.group(1))
            rendered.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            index += 1
        elif BULLET.fullmatch(line) or NUMBERED.fullmatch(line):
            index, block = _listed(lines, index)
            rendered.append(block)
        elif QUOTE.fullmatch(line):
            index, block = _quoted(lines, index)
            rendered.append(block)
        else:
            index, block = _paragraph(lines, index)
            rendered.append(block)
    return "\n".join(rendered)


def _fenced(lines: Sequence[str], index: int) -> tuple[int, str]:
    info = lines[index].strip().removeprefix(FENCE).strip()
    body: list[str] = []
    index += 1
    while index < len(lines) and not lines[index].strip().startswith(FENCE):
        body.append(lines[index])
        index += 1
    language = f' class="language-{html.escape(info, quote=True)}"' if info.isidentifier() else ""
    code = html.escape("\n".join(body))
    return index + 1, f"<pre><code{language}>{code}</code></pre>"


def _listed(lines: Sequence[str], index: int) -> tuple[int, str]:
    tag = "ul" if BULLET.fullmatch(lines[index].strip()) else "ol"
    items: list[str] = []
    while index < len(lines):
        line = lines[index].strip()
        match = BULLET.fullmatch(line) or NUMBERED.fullmatch(line)
        if match is None:
            break
        items.append(f"<li>{_inline(match.group(1))}</li>")
        index += 1
    return index, f"<{tag}>" + "".join(items) + f"</{tag}>"


def _quoted(lines: Sequence[str], index: int) -> tuple[int, str]:
    said: list[str] = []
    while index < len(lines) and (match := QUOTE.fullmatch(lines[index].strip())) is not None:
        said.append(_inline(match.group(1)))
        index += 1
    return index, "<blockquote>" + LINE_BREAK.join(said) + "</blockquote>"


def _paragraph(lines: Sequence[str], index: int) -> tuple[int, str]:
    said: list[str] = []
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith(FENCE) or QUOTE.fullmatch(line) or HEADING.fullmatch(line):
            break
        if BULLET.fullmatch(line) or NUMBERED.fullmatch(line):
            break
        said.append(_inline(line))
        index += 1
    return index, "<p>" + LINE_BREAK.join(said) + "</p>"


def _inline(text: str) -> str:
    return INLINE.sub(_marked, html.escape(text, quote=True))


def _marked(match: re.Match[str]) -> str:
    """One inline span of already-escaped words. A span's own words go back through the same rules,
    so emphasis inside a link or a link inside emphasis is written as written."""
    if (code := match.group("code")) is not None:
        return f"<code>{code}</code>"
    if (href := match.group("href")) is not None:
        text = INLINE.sub(_marked, match.group("text"))
        return f'<a href="{href}">{text}</a>' if _followable(href) else text
    if (strong := match.group("strong")) is not None:
        return f"<strong>{INLINE.sub(_marked, strong)}</strong>"
    emphasized = match.group("italic") or match.group("under") or ""
    return f"<em>{INLINE.sub(_marked, emphasized)}</em>"


def _followable(href: str) -> bool:
    scheme, separator, _ = href.partition(":")
    return bool(separator) and scheme.lower() in LINK_SCHEMES


def _blocks(markdown: str) -> list[str]:
    """The reply's paragraphs, in order. A fenced block is one paragraph however many blank lines
    it holds, so nothing splits code that was written together."""
    found: list[str] = []
    held: list[str] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith(FENCE):
            if held:
                found.append("\n".join(held))
                held = []
            fence = [line]
            index += 1
            while index < len(lines):
                fence.append(lines[index])
                closed = lines[index].strip().startswith(FENCE)
                index += 1
                if closed:
                    break
            found.append("\n".join(fence))
            continue
        if not line.strip():
            if held:
                found.append("\n".join(held))
                held = []
        else:
            held.append(line)
        index += 1
    if held:
        found.append("\n".join(held))
    return found


def _bounded(block: str, budget: int) -> list[str]:
    """One paragraph as the pieces it fits the budget in. A fenced block is reopened with the same
    info string in each piece after the first, so each piece is a fence a client can close."""
    if len(block.encode()) <= budget:
        return [block]
    lines = block.splitlines()
    opener = lines[0].strip() if block.strip().startswith(FENCE) else ""
    fence = f"{opener}\n" if opener else ""
    if opener:
        closed = len(lines) > 1 and lines[-1].strip().startswith(FENCE)
        lines = lines[1:-1] if closed else lines[1:]
    pieces: list[str] = []
    held: list[str] = []
    line_budget = max(1, budget - len(fence.encode()) - len(FENCE.encode()) - 1)
    for line in lines:
        for piece in _cut(line, line_budget):
            if held and _piece_bytes([*held, piece], fence) > budget:
                pieces.append(_refenced(held, fence))
                held = []
            held.append(piece)
    if held:
        pieces.append(_refenced(held, fence))
    return pieces


def _piece_bytes(held: Sequence[str], fence: str) -> int:
    """The bytes one piece takes — opener, lines, closing fence — the accounting `_bounded` flushes
    against, so a part never exceeds the budget by an uncounted newline."""
    body = len("\n".join(held).encode())
    if fence:
        return len(fence.encode()) + body + len(FENCE) + 1
    return body


def _refenced(lines: Sequence[str], fence: str) -> str:
    body = "\n".join(lines)
    return f"{fence}{body}\n{FENCE}" if fence else body


def _cut(line: str, budget: int) -> list[str]:
    """One line as the runs of it that fit, cut at a space where one does."""
    pieces: list[str] = []
    rest = line
    while len(rest.encode()) > budget:
        head = rest.encode()[:budget].decode(errors="ignore")
        space = head.rfind(" ")
        head = head[:space] if space > 0 else head
        if not head:
            head = rest[0]
        pieces.append(head)
        rest = rest[len(head) :]
    if rest or not pieces:
        pieces.append(rest)
    return pieces


def _packed(blocks: Sequence[str], budget: int) -> list[str]:
    """As many whole paragraphs to a part as the budget holds, in order."""
    packed: list[str] = []
    for block in blocks:
        if packed and len(f"{packed[-1]}\n\n{block}".encode()) <= budget:
            packed[-1] = f"{packed[-1]}\n\n{block}"
        else:
            packed.append(block)
    return packed
