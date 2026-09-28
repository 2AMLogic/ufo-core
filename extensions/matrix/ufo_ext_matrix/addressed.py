"""Whether one room line is addressed to the bot, and so bypasses the ambient decision.

A group room is mostly people talking to each other. Four things make a line the bot's: the
intentional mention its sender's client recorded, the pill a client without intentional mentions
rendered into the HTML, the bot's own name in the words, and a reply to something the bot said. Each
matches the bot's identity whole, so `@ufobot` addresses nobody named `@ufo`.

Nothing here imports `ufo` or an HTTP client, so the contract tests hold these rules on a checkout
with neither installed."""

import re
from dataclasses import dataclass

from ufo_ext_matrix.events import RoomMessage

PERMALINK_ROOT = "https://matrix.to/#/"


@dataclass(frozen=True)
class Bot:
    """The bot as one room names it: its MXID, the display name a member types instead of the MXID,
    and the messages of its own the room has lately carried — what a reply to the bot replies to.
    A stream that has heard the bot say nothing in a room knows none, and a reply there is a reply
    to a member."""

    mxid: str
    display_name: str = ""
    said: frozenset[str] = frozenset()


def addresses(message: RoomMessage, bot: Bot) -> bool:
    """Whether the sender addressed the bot rather than the room."""
    return (
        bot.mxid in message.mentions
        or pills(message.formatted_body, bot.mxid)
        or names(message.body, bot.mxid)
        or (bool(bot.display_name) and names(message.body, bot.display_name))
        or message.replying_to in bot.said
    )


def pills(formatted_body: str, mxid: str) -> bool:
    """Whether the HTML links to `mxid` itself rather than to an MXID on a longer server name."""
    pattern = rf"{re.escape(PERMALINK_ROOT + mxid)}(?=[\"'?/<>\s]|$)"
    return re.search(pattern, formatted_body) is not None


def names(body: str, name: str) -> bool:
    """Whether the words hold `name` as a name of its own rather than inside a longer one."""
    pattern = rf"(?<![\w:.\-]){re.escape(name)}(?![\w.\-])"
    return re.search(pattern, body, re.IGNORECASE) is not None
