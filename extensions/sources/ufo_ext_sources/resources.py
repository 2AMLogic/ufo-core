"""What a link names, and the clauses that wake a conversation about it.

A trigger is a list of JMESPath clauses over the connection's changed pages, and nobody types one:
the offer on a seen link writes the clauses for the resource it names, and a trigger that names none
is resolved at apply to the provider's own minimal set. Each provider owns those two rules — what
its records look like is its to know — and `RESOURCE_RULES` is the table this module dispatches on.

A provider absent from it names no resource, so a link to it earns no offer, and its whole-feed
default is `` `true` ``: every changed page meets it once, which under the start-to-meet rule is
once per new page. A promise nothing can read wakes on arrival rather than on everything."""

from collections.abc import Callable
from dataclasses import dataclass

from ufo_ext_sources.providers import github

ANY_PAGE = "`true`"
"""The whole-feed clauses of a provider with no rules: one clause every page meets, which wakes on
each page the first time it is seen and never again."""


@dataclass(frozen=True)
class ResourceRules:
    """One provider's rules. `canonical` reads a link and answers the one URL the resource is stored
    under, or None for a link that names nothing a trigger narrows to. `identity` answers what makes
    two canonical URLs one thing, since GitHub numbers a repository's pull requests and issues in
    one sequence and a page of links spells one of them both ways. `scope` reads the catalog entry a
    link hangs under, spelled as that link spells it, and `clauses` writes the clauses that wake a
    conversation about one resource under that spelling, one per event. `wakes` is the provider's
    minimal set for a whole feed."""

    canonical: Callable[[str], str | None]
    identity: Callable[[str], str]
    scope: Callable[[str], str | None]
    clauses: Callable[[str, str], tuple[str, ...]]
    wakes: tuple[str, ...]


RESOURCE_RULES: dict[str, ResourceRules] = {
    github.GitHubConnector.name: ResourceRules(
        canonical=github.resource_url,
        identity=github.resource_identity,
        scope=github.resource_repo,
        clauses=github.resource_clauses,
        wakes=github.WAKE_CLAUSES,
    ),
}


def canonical_resource(provider: str, url: str) -> str | None:
    """The URL a narrowed trigger on this provider stores for `url`, or None where the link names
    nothing the provider's rules read."""
    rules = RESOURCE_RULES.get(provider)
    return None if rules is None else rules.canonical(url)


def resource_identity(provider: str, resource: str) -> str:
    """What one canonical resource is one thing under: two resources sharing it are one thing under
    two spellings, as GitHub's `pull/7` and `issues/7` are. A resource a provider's rules do not
    read is its own identity."""
    rules = RESOURCE_RULES.get(provider)
    return resource if rules is None else rules.identity(resource)


def resource_scope(provider: str, url: str) -> str | None:
    """The catalog entry a link hangs under — a GitHub repository — spelled as the link spells it,
    which is the spelling the provider's own records carry when the link was copied from it."""
    rules = RESOURCE_RULES.get(provider)
    return None if rules is None else rules.scope(url)


def resource_clauses(provider: str, resource: str, scope: str) -> tuple[str, ...]:
    """The `when` a trigger on one resource of this provider is written with — one clause per event
    the watch reports, so a page moving from one wanted state to another flips a clause of its own
    and wakes the conversation again. `scope` is the catalog entry the link spelled, since the
    canonical resource case-folds it and no record carries the folded form."""
    rules = RESOURCE_RULES.get(provider)
    return () if rules is None else rules.clauses(resource, scope)


def default_clauses(provider: str) -> tuple[str, ...]:
    """The `when` a trigger on this provider's whole feed resolves to when it names none. Stored
    resolved, so every row carries concrete clauses and a read of the trigger shows what it does."""
    rules = RESOURCE_RULES.get(provider)
    return (ANY_PAGE,) if rules is None else rules.wakes
