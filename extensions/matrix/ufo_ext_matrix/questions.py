"""A terminal `ask_user` as a room reads it, and one reply as the answer it names.

A room has no buttons, so a question is a numbered list. An ask of one question is answered by a
number; an ask of several labels each option with its question, `1a` to `2b`, so one reply answers
them all. A poll carries the same labels as its answer ids, so a tap and a typed label arrive as the
one choice.

A reply answers only where it is labels and nothing else — `2`, or `1a 2b`, or `1, 3` for a question
that takes several. Anything else is words, which is how a member answers a question that asked for
words rather than a choice, and how they say something that merely begins with a number.

Nothing here imports `ufo` or an HTTP client, so the contract tests hold these rules on a checkout
with neither installed."""

import re
import string
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

ALPHABET = string.ascii_lowercase
LABEL = re.compile(r"(\d{1,3})([a-z]{0,2})")
BETWEEN = re.compile(r"[,;/&]|\s+|\band\b")
POLL_KIND = "m.poll.disclosed"
CHOSEN_MARK = "✓"
ONE_HINT = "Reply with a number, for example 1."
SEVERAL_HINT = "Reply with the numbers you choose, for example 1, 3."
LABELLED_HINT = "Reply with one label per question, for example 1a 2b."


@dataclass(frozen=True)
class Asked:
    """One question of an ask as a room renders it: its words, the choices it offers, and whether it
    takes more than one of them. A question that offers none asks for words."""

    question: str
    options: tuple[str, ...] = ()
    multi_select: bool = False


@dataclass(frozen=True)
class Choice:
    """One option a reply named: which question of the ask, and which of that question's options."""

    question: int
    option: int


def label(question: int, option: int, questions: int) -> str:
    """The label one option answers to: the option's own number where the ask holds one question,
    and the question's number with the option's letter where it holds several."""
    return str(option + 1) if questions == 1 else f"{question + 1}{letters(option)}"


def letters(option: int) -> str:
    """An option's letter: `a` through `z`, then `aa`, as a column is named."""
    name = ""
    while option >= 0:
        option, remainder = divmod(option, len(ALPHABET))
        name = ALPHABET[remainder] + name
        option -= 1
    return name


def option_of(name: str) -> int:
    """The option a letter names, the inverse of `letters`."""
    index = 0
    for character in name:
        index = index * len(ALPHABET) + ALPHABET.index(character) + 1
    return index - 1


def question_block(title: str, asked: Sequence[Asked]) -> str:
    """The whole ask as one message: its heading, each question with its labelled options, and one
    line saying how to answer. A question that asks for words is its own words alone."""
    written = [title.strip()] if title.strip() else []
    for index, question in enumerate(asked):
        lines = [question.question]
        lines += [
            f"{label(index, option, len(asked))}. {text}"
            for option, text in enumerate(question.options)
        ]
        written.append("\n".join(lines))
    hint = answering_hint(asked)
    if hint:
        written.append(hint)
    return "\n\n".join(written)


def answering_hint(asked: Sequence[Asked]) -> str:
    """The one line that says how to answer this ask, and nothing where it offers no choice."""
    if not any(question.options for question in asked):
        return ""
    if len(asked) > 1:
        return LABELLED_HINT
    return SEVERAL_HINT if asked[0].multi_select else ONE_HINT


def settled_block(title: str, asked: Sequence[Asked], chosen: Sequence[Choice]) -> str:
    """The ask rewritten as the answer that landed: the same list with a mark beside what was
    chosen, and no line asking for an answer the question already holds."""
    taken = {(choice.question, choice.option) for choice in chosen}
    written = [title.strip()] if title.strip() else []
    for index, question in enumerate(asked):
        lines = [question.question]
        for option, text in enumerate(question.options):
            mark = f" {CHOSEN_MARK}" if (index, option) in taken else ""
            lines.append(f"{label(index, option, len(asked))}. {text}{mark}")
        written.append("\n".join(lines))
    return "\n\n".join(written)


def answer_words(asked: Sequence[Asked], chosen: Sequence[Choice]) -> str:
    """The inbound words one answer becomes: the labels the member chose, said as the options they
    name. An ask of one question is answered by those options alone; an ask of several names each
    question beside its own answer, since which question was answered is the answer."""
    said = []
    for index, question in enumerate(asked):
        options = [question.options[choice.option] for choice in chosen if choice.question == index]
        if not options:
            continue
        joined = ", ".join(options)
        said.append(joined if len(asked) == 1 else f"{question.question}: {joined}")
    return "\n".join(said)


def choices(body: str, asked: Sequence[Asked]) -> tuple[Choice, ...]:
    """The options one reply names, in the order it names them, and none for words: every token of
    the reply is a label of `asked` or the whole reply is words. A question that takes one choice
    keeps the first its reply names, and a label named twice counts once."""
    if not any(question.options for question in asked):
        return ()
    tokens = [token for token in BETWEEN.split(body.strip().lower()) if token]
    if not tokens:
        return ()
    kept: list[Choice] = []
    for token in tokens:
        match = LABEL.fullmatch(token)
        choice = None if match is None else _named(match.group(1), match.group(2), asked)
        if choice is None:
            return ()
        one = not asked[choice.question].multi_select
        if choice in kept or (one and any(c.question == choice.question for c in kept)):
            continue
        kept.append(choice)
    return tuple(kept)


def poll_choices(answers: Sequence[str], asked: Sequence[Asked]) -> tuple[Choice, ...]:
    """The options a poll response names, read as the labels the poll's answer ids carry. A response
    to somebody else's poll names ids these labels do not, and answers nothing."""
    return choices(" ".join(answers), asked)


def poll_content(
    title: str, asked: Asked, relates_to: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One question as a poll a member taps: the question's words, its options under the same labels
    the message wrote them under, and the words themselves for a client that draws no poll."""
    return {
        "m.poll": {
            "question": _text(asked.question),
            "kind": POLL_KIND,
            "max_selections": 1,
            "answers": [
                {"m.id": label(0, option, 1), **_text(text)}
                for option, text in enumerate(asked.options)
            ],
        },
        **_text(question_block(title, (asked,))),
        **({"m.relates_to": dict(relates_to)} if relates_to else {}),
    }


def _text(body: str) -> dict[str, Any]:
    return {"m.text": [{"body": body}]}


def _named(number: str, letter: str, asked: Sequence[Asked]) -> Choice | None:
    """The option one label names, or None where the ask offers no such option. A bare number names
    an option of the only question there is; an ask of several is answered by label alone, since a
    number on its own says nothing about which question it answers."""
    if letter:
        question, option = int(number) - 1, option_of(letter)
    elif len(asked) == 1:
        question, option = 0, int(number) - 1
    else:
        return None
    if not 0 <= question < len(asked) or not 0 <= option < len(asked[question].options):
        return None
    return Choice(question=question, option=option)
