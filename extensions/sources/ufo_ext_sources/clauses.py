"""The clauses a trigger's `when` is, and what it takes to decide one against a changed page.

A clause is a JMESPath expression over one document — the page's `stream`, its `title`, and `page`,
the provider's record parsed out of the body. JMESPath because it is already in the tree through
botocore, has no loops, no recursion and no side effects, so a clause costs time linear in the
document, and agents know it from the AWS CLI `--query` flag.

A clause is refused at apply if it does not parse or is longer than `WHEN_CLAUSE_MAX_CHARS`, and a
list of more than `WHEN_CLAUSES_MAX` is refused whole — bounds at the one place a member's text
enters. A clause that parses can still raise when it runs, because JMESPath types its functions at
call time and a record need not carry the field the clause reaches for: that raises `ClauseFailed`,
which pauses the one trigger with the error on its row rather than failing the hook, since a hook
that raises leaves the `page_change` cursor unadvanced and replays the batch for every trigger in
the workspace on every tick."""

import json
from dataclasses import dataclass

import jmespath
from jmespath.exceptions import JMESPathError
from jmespath.parser import ParsedResult

WHEN_CLAUSES_MAX = 16
WHEN_CLAUSE_MAX_CHARS = 1000
_RECORD_SEPARATOR = "\n\n"


class ClauseFailed(Exception):
    """One clause raised while being evaluated. Carries the clause and the parser's own message, in
    the words the member reads off the paused trigger."""


@dataclass(frozen=True)
class Clauses:
    """One trigger's `when`, parsed. Holds the source text beside each expression because the text
    is what a refusal names and what the row stores."""

    source: tuple[str, ...]
    parsed: tuple[ParsedResult, ...]

    def met(self, stream: str, title: str, body: str) -> tuple[bool, ...]:
        """Which clauses this changed page meets, one answer per clause in order. A result of
        `false`, `null`, an empty string, an empty list or an empty object is no match, which is
        JMESPath's own rule.

        Anything a clause raises becomes `ClauseFailed`, not `JMESPathError` alone: `contains`
        evaluates `search in subject`, so a clause comparing a string against a number parses,
        passes apply, and raises a plain TypeError on the first page. Whatever the type, the fault
        belongs to the one trigger that named the clause — the hook's failure scope is the batch, so
        an error reaching it holds every trigger in the workspace and meets the same clause on every
        retry."""
        document = {"stream": stream, "title": title, "page": page_record(body)}
        met: list[bool] = []
        for source, expression in zip(self.source, self.parsed, strict=True):
            try:
                met.append(bool(expression.search(document)))
            except Exception as error:
                raise ClauseFailed(f"{source!r} failed on {stream}: {error}") from error
        return tuple(met)


def compile_when(clauses: tuple[str, ...]) -> Clauses:
    """The clauses of a `when`, parsed, or a `ValueError` naming the one that is wrong. Called at
    apply so a trigger that cannot be evaluated is never written, and again in the hook, where the
    row's own clauses parse by construction."""
    if not clauses:
        raise ValueError("a source trigger names at least one clause")
    if len(clauses) > WHEN_CLAUSES_MAX:
        raise ValueError(
            f"a source trigger names at most {WHEN_CLAUSES_MAX} clauses, not {len(clauses)}"
        )
    parsed: list[ParsedResult] = []
    for clause in clauses:
        if len(clause) > WHEN_CLAUSE_MAX_CHARS:
            raise ValueError(
                f"a clause is at most {WHEN_CLAUSE_MAX_CHARS} characters, not {len(clause)}"
            )
        try:
            parsed.append(jmespath.compile(clause))
        except JMESPathError as error:
            raise ValueError(f"{clause!r} is not a JMESPath expression: {error}") from error
    return Clauses(source=clauses, parsed=tuple(parsed))


def page_record(body: str) -> object:
    """The provider's record out of a page body, which is the heading the connector renders over
    the record's JSON. None for a body that carries no JSON object — a tombstone, a folder source's
    file text — so a clause reaching into `page` sees null and answers as JMESPath does."""
    _, separator, record = body.partition(_RECORD_SEPARATOR)
    if not separator:
        return None
    try:
        return json.loads(record)
    except json.JSONDecodeError:
        return None
