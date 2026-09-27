"""The spans a round marks and the Markdown links that carry workspace files.

`marked_replies` reads the reply spans out of a completed round's text and returns the text the
window keeps, the same words with the markup gone. `marked_artifacts` reads workspace file links
out of a closing answer and returns the answer with each link reduced to its label. `SpanRedaction`
applies the same projection incrementally wherever a model stream splits. Malformed reply markup
reaches no member; incomplete Markdown remains prose when the stream ends."""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from uuid import UUID

REPLY_TAG = "reply-to"
REPLY_CLOSER = f"</{REPLY_TAG}>"
REPLY_OPENER = re.compile(rf'<{REPLY_TAG}\s+message="([^"<>]*)"\s*>')
REPLY_SPAN = re.compile(f"{REPLY_OPENER.pattern}(.*?){re.escape(REPLY_CLOSER)}", re.DOTALL)
REPLY_MARKUP = re.compile(rf"<{REPLY_TAG}(?:\s[^<>]*)?>|{re.escape(REPLY_CLOSER)}")
REPLY_HEAD = f"<{REPLY_TAG}"
REPLY_PARTIAL = re.compile(rf"<{REPLY_TAG}\s[^<>]*")

ARTIFACT_FALLBACK_NAME = "artifact"
ARTIFACT_DEFAULT_SUFFIX = ".md"
ARTIFACT_TEXT_MAX_CHARS = 80
FILE_LINK = re.compile(r"(?<![\w!`])\[([^\]\n]*)\]\(\s*(?:<([^>\n]+)>|([^)\s]+))\s*\)")
LINK_TAIL = re.compile(r"\[[^\]\n]*(?:\](?:\([^)\s]*)?)?$")
LINK_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass(frozen=True)
class MarkedReply:
    """One span of a round's text the model addressed to a member: the words to deliver and the
    `message_ref` the tag named, or None when it named something that is not a message id."""

    message_ref: UUID | None
    text: str


@dataclass(frozen=True)
class MarkedArtifact:
    """One workspace file a closing answer linked, its download name, and the bounded words the
    member clicks to open it, None when the link named none and the surface says its own."""

    name: str
    path: str
    text: str | None = None


def marked_replies(text: str) -> tuple[tuple[MarkedReply, ...], str]:
    """The round's closed reply spans in the order the model produced them, and the round's text
    with every trace of the markup removed. A span's words stay in that text: the window records
    what the turn told the member, the way an ordinary assistant message records a closing reply."""
    replies: list[MarkedReply] = []
    for match in REPLY_SPAN.finditer(text):
        spoken = REPLY_MARKUP.sub("", match.group(2)).strip()
        if spoken:
            replies.append(MarkedReply(message_ref=_named_message(match.group(1)), text=spoken))
    return tuple(replies), REPLY_MARKUP.sub("", text)


def marked_artifacts(text: str) -> tuple[tuple[MarkedArtifact, ...], str]:
    """The linked workspace files in written order and the answer with only their labels."""
    artifacts: list[MarkedArtifact] = []

    def link(match: re.Match[str]) -> str:
        target = match.group(2) or match.group(3)
        if not _file_target(target):
            return match.group(0)
        artifacts.append(
            MarkedArtifact(
                name=_artifact_name(target), path=target, text=_link_text(match.group(1))
            )
        )
        return match.group(1)

    delivered = FILE_LINK.sub(link, text)
    return tuple(artifacts), delivered


def _named_message(named: str) -> UUID | None:
    try:
        return UUID(named.strip())
    except ValueError:
        return None


def _artifact_name(named: str) -> str:
    leaf = PurePosixPath(named.strip().replace("\\", "/")).name
    name = leaf if leaf not in ("", ".", "..") else ARTIFACT_FALLBACK_NAME
    return name if "." in name else name + ARTIFACT_DEFAULT_SUFFIX


def _file_target(target: str) -> bool:
    return not (LINK_SCHEME.match(target) or target.startswith(("#", "//")))


def _link_text(named: str) -> str | None:
    text = " ".join(named.split())
    return text if 0 < len(text) <= ARTIFACT_TEXT_MAX_CHARS else None


@dataclass
class SpanRedaction:
    """Project reply spans and workspace links consistently from arbitrary model chunks."""

    held: str = field(default="")
    inside: bool = field(default=False)
    previous: str = field(default="")

    def feed(self, chunk: str) -> str:
        self.held += chunk
        published: list[str] = []
        while True:
            if self.inside:
                cut = self.held.find(REPLY_CLOSER)
                if cut == -1:
                    self.held = _growing_suffix(self.held, REPLY_CLOSER)
                    break
                self.held = self.held[cut + len(REPLY_CLOSER) :]
                self.inside = False
                continue
            opened = REPLY_OPENER.search(self.held)
            if opened is not None:
                published.append(self.held[: opened.start()])
                self.held = self.held[opened.end() :]
                self.inside = True
                continue
            settled = _settled_chars(self.held)
            published.append(self.held[:settled])
            self.held = self.held[settled:]
            break
        text = REPLY_MARKUP.sub("", "".join(published))
        if not text:
            return ""
        linked = FILE_LINK.sub(
            lambda match: (
                match.group(1) if _file_target(match.group(2) or match.group(3)) else match.group(0)
            ),
            self.previous + text,
        )[len(self.previous) :]
        self.previous = text[-1]
        return linked

    def finish(self) -> str:
        if self.inside or _settled_chars(self.held) < len(self.held):
            visible = self.held if self.held.startswith("[") else ""
        else:
            visible = self.held
        self.held = ""
        self.inside = False
        return visible


def _growing_suffix(text: str, token: str) -> str:
    """The longest tail of `text` that a following chunk could complete into `token`."""
    for start in range(max(len(text) - len(token) + 1, 0), len(text)):
        if token.startswith(text[start:]):
            return text[start:]
    return ""


def _settled_chars(text: str) -> int:
    """The prefix settled as prose rather than a partial reply span or file link."""
    cut = len(text)
    angle = text.rfind("<")
    if angle != -1:
        tail = text[angle:]
        if (
            REPLY_HEAD.startswith(tail)
            or REPLY_CLOSER.startswith(tail)
            or REPLY_PARTIAL.fullmatch(tail)
        ):
            cut = angle
    growing = LINK_TAIL.search(text[:cut])
    return growing.start() if growing is not None else cut
