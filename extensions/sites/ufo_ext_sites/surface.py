"""The hosted-site frame: the page a site's permanent link opens, and the creator's visibility
control.

The link is an address, not an authorization — a surface token carrying `{ws, conversation, name}`,
so the shared fleet resolves the workspace from the URL alone before a row is read, and nothing in
it expires. Every visit passes the gate again: the token is verified, the site resolved, the viewer
authenticated from the `ufo_session` cookie the web surface binds, and the site's `visibility`
decides. A public site skips authentication; a workspace site admits any authenticated member; a
private one admits its creator and workspace admins. A site bound as an agent's homepage answers
on the agent instead: a workspace-visible agent's homepage admits any authenticated member, a
private agent's its owner and admins. The portal's signed embed link redirects its sandboxed iframe
to ingress; the permanent site link keeps the frame around it. Anything else is a 404 with the same
body as an unknown token,
so the frame is no oracle for which sites exist. An unauthenticated viewer of a non-public site is
told exactly that. On a deploy with a browser sign-in (`[serve] sign_in_path`) the page links
`[sites] sign_in_url`, or that path where it is unset; the walk it starts ends by binding
`ufo_session`, so signing in is the whole recovery — a member of the workspace needs nothing widened
to see a site their workspace already may. The portal itself is not the link: reached cold it can
only ask for a token the viewer does not have. A deploy with no sign-in binds no session, so the
page says that only a public site opens in a browser.

The bytes are not served here. A site's permanent link renders an `<iframe>` at the site's own
ingress origin; a portal homepage link redirects the portal's sandboxed iframe there after the same
gate. A deploy-wide app bundle uses the same stable redirect only to select its slug and digest; its
code is public and takes no viewer or agent gate. The ingress address is minted per visit and never
stored: it carries a view token the ingress trades for the origin's own session cookie. That token
is a bearer credential until its TTL passes, not a single use, and the origin session it buys
consults no `hosted_site` row — so narrowing a site's visibility or deleting it stops new viewers,
not one already inside. The site's own origin keeps its scripts away from the selector, the portal
session cookie, and every app route. The permanent link's `sandbox` and the portal homepage iframe
each keep model-authored bytes from navigating the member's tab away; the visibility `POST` still
carries a CSRF token bound to the viewer's own session, so a cross-site form cannot flip a site the
creator owns.

The head carries a share card, so a link pasted into a chat unfurls as a picture rather than one
bare line. The site's own name reaches that head only when the site is public: the crawler that
reads these tags carries no session and passes no gate, so a name in them is published to whoever
holds the link. Every other level gets a generic title naming neither the site nor its workspace.

The picture obeys that same rule, harder. A public site's card is the site's own front page beside
the brandmark, composed at deploy time (`share_card.py`) and served by the one anonymous route here:
`share/site/<site token>/<digest>.jpg`. That route re-reads the row and serves the card only while
the site is still public and not homepage-bound, because a level is mutable and a URL in a head tag
cannot be recalled. It carries no session, sets no cookie, mints no grant, and answers
`public, max-age=600` rather than `immutable`, so a site that stops being public stops being
previewed within minutes; a cache in front of the route keeps its copy for exactly that lifetime, so
the same minutes bound the drain. Every other site unfurls as the generic card, which names
nothing: `[sites] share_card_url` where the deploy names one, else the card bundled in `assets/`,
served by the same route under its own digest off the deploy's own host."""

import hashlib
import html
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ufo.sdk.bearer import SESSION_COOKIE, verify_token
from ufo.sdk.http import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Request,
    Response,
)
from ufo.sdk.sandbox import (
    INGRESS_SESSION_ENDED_MESSAGE,
    serve_port,
    shipped_anchor,
    shipped_app_slug,
)
from ufo.sdk.seats import Seats
from ufo.sdk.surface_token import mint_surface_token, verify_surface_token
from ufo.sdk.surfaces import SurfaceAuth, SurfaceContext, SurfaceRoute, SurfaceSpec
from ufo_ext_sites.share_card import CARD_EXTENSION, CARD_HEIGHT, CARD_MEDIA_TYPE, CARD_WIDTH
from ufo_ext_sites.store import HostedSite, HostedSites, Visibility, visibility_level

SURFACE_SITES = "sites"
FRAME_PATH = "/surface/sites"
WORKSPACE_CLAIM = "ws"
CONVERSATION_CLAIM = "conversation"
NAME_CLAIM = "name"
SLUG_CLAIM = "slug"
DIGEST_CLAIM = "digest"
CSRF_CLAIM = "csrf"
PORTAL_EMBED_CLAIM = "portal_embed"
PORTAL_EMBED_VALUE = "1"
FETCH_DESTINATION_HEADER = "sec-fetch-dest"
IFRAME_DESTINATION = "iframe"

TOKEN_PARAM = "site_token"
PATH_PARAM = "site_path"
CARD_HASH_PARAM = "card_hash"
VISIBILITY_FIELD = "visibility"
CSRF_FIELD = "csrf"
HOSTING_UNCONFIGURED = (
    "this deployment sets no [connect] public_base_url, so a site has no link to be opened at"
)
NOT_FOUND_BODY = "no such site"
UNCONFIGURED_BODY = "Site hosting is not configured on this deployment."
CSRF_REJECTED_BODY = "the visibility form did not match this session; reload the page and retry"
HOMEPAGE_FOLLOWS_AGENT_BODY = "this site is an agent's homepage; its visibility follows the agent's"
NOT_SIGNED_IN_PAGE = (
    "<main><p>This site is not public, and this browser is not signed in to the workspace that "
    'hosts it.</p><p><a href="{sign_in}">Sign in</a>, then open this link again.</p></main>'
)
NO_BROWSER_SIGN_IN = "This deploy has no browser sign-in; only public sites open in a browser."
NO_SIGN_IN_PAGE = f"<main><p>{NO_BROWSER_SIGN_IN}</p></main>"
NOT_PUBLIC_TITLE = "Not public"
NO_PORTAL_TITLE = "No portal"
NO_PORTAL_PAGE = (
    "<main><p>This app's page is read in the member portal, and this deployment installs "
    "none.</p></main>"
)
AGENT_FRAGMENT = "#/agents/"
"""The portal's own address for one app's screen. Core does not interpret a fragment, so the
route the portal reads is spelled here, beside the one redirect that sends a viewer to it."""
IFRAME_SANDBOX = (
    "allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads "
    "allow-pointer-lock"
)
FOCUS_REFRESH_SCRIPT = (
    "<script>const frame=document.currentScript.previousElementSibling;let ended=false;"
    "const refresh=()=>{if(ended)location.reload()};"
    "addEventListener('message',event=>{"
    f"if(event.source!==frame.contentWindow||event.data?.ufo!=='{INGRESS_SESSION_ENDED_MESSAGE}')"
    "return;ended=true;if(document.hasFocus())refresh()});"
    "addEventListener('focus',refresh)</script>"
)
SHARE_CARD = (Path(__file__).parent / "assets" / "share-card.jpg").read_bytes()
SHARE_CARD_DIGEST = hashlib.sha256(SHARE_CARD).hexdigest()
SHARE_CARD_WIDTH = str(CARD_WIDTH)
SHARE_CARD_HEIGHT = str(CARD_HEIGHT)
SHARE_CARD_ALT = "The words Made with above the UFO mark and wordmark, on a black field."
SITE_CARD_SEGMENT = "share/site"
SITE_CARD_PATH = f"{FRAME_PATH}/{SITE_CARD_SEGMENT}"
SITE_CARD_CACHE = "public, max-age=600"
"""Cacheable, and never `immutable`: a site that stops being public must stop being previewed within
minutes, and the digest in the URL already covers the redeploy case.

A cache in front of this route stores its copy under this directive, so this is the one place the
drain window is set — for that cache and the browser alike."""
SITE_CARD_HEADERS = {"cache-control": SITE_CARD_CACHE, "x-content-type-options": "nosniff"}
SITE_CARD_ALT = "The front page of {name}, drawn beside the UFO brandmark."
GENERIC_SHARE_TITLE = "A site on UFO"
SHARE_DESCRIPTION = "A site made with UFO."
VISIBILITY_LABELS: dict[Visibility, str] = {
    "private": "Only me",
    "workspace": "Workspace members",
    "public": "Anyone with the link",
}
VISIBILITY_BADGES: dict[Visibility, str] = {
    "private": "Private",
    "workspace": "Visible to workspace members",
    "public": "Visible to anyone with the link",
}


class SiteHostingUnconfigured(RuntimeError):
    """The deploy configures no public base URL, so no site link exists to mint or report."""


@dataclass(frozen=True)
class SiteAddress:
    """What a site token names, including whether the portal minted it for its own iframe."""

    workspace_id: UUID
    conversation_id: UUID
    name: str
    portal_embed: bool


def site_token(workspace_id: UUID, conversation_id: UUID, name: str) -> str:
    """The permanent token addressing one site, mintable wherever a site is registered."""
    return mint_surface_token(
        SURFACE_SITES,
        {
            WORKSPACE_CLAIM: str(workspace_id),
            CONVERSATION_CLAIM: str(conversation_id),
            NAME_CLAIM: name,
        },
    )


def site_url(
    public_base_url: str | None, workspace_id: UUID, conversation_id: UUID, name: str
) -> str:
    """The site's hosted link: the frame route on the deploy's public base, carrying the address.
    The one producer of that link, and it refuses to be approximate — a deploy with no
    `[connect] public_base_url` can host nothing, and the deliverable every tool description and
    prompt promises must not come back absent or null."""
    if not public_base_url:
        raise SiteHostingUnconfigured(HOSTING_UNCONFIGURED)
    token = site_token(workspace_id, conversation_id, name)
    return f"{public_base_url.rstrip('/')}{FRAME_PATH}/{token}"


@dataclass(frozen=True)
class ShippedAddress:
    """What a shipped-frame token names: its workspace, app slug, and bundle digest."""

    workspace_id: UUID
    slug: str
    digest: str


def shipped_homepage_url(
    public_base_url: str | None, workspace_id: UUID, slug: str, digest: str
) -> str | None:
    """The stable portal embed link for a workspace's deploy-wide app bundle."""
    if not public_base_url:
        return None
    token = mint_surface_token(
        SURFACE_SITES,
        {
            WORKSPACE_CLAIM: str(workspace_id),
            SLUG_CLAIM: slug,
            DIGEST_CLAIM: digest,
            PORTAL_EMBED_CLAIM: PORTAL_EMBED_VALUE,
        },
    )
    return f"{public_base_url.rstrip('/')}{FRAME_PATH}/{token}"


def shipped_address(token: str) -> ShippedAddress | None:
    """The shipped address a token proves, or None when its signature, surface, or claims do not
    hold — a normal site token (no `slug` or `digest`) reads as None here, and a shipped one
    reads as None in `site_address`, so the two token shapes never collide."""
    claims = verify_surface_token(SURFACE_SITES, token)
    if claims is None or claims.get(PORTAL_EMBED_CLAIM) != PORTAL_EMBED_VALUE:
        return None
    try:
        return ShippedAddress(
            workspace_id=UUID(claims[WORKSPACE_CLAIM]),
            slug=claims[SLUG_CLAIM],
            digest=claims[DIGEST_CLAIM],
        )
    except (KeyError, ValueError):
        return None


def site_card_url(public_base_url: str | None, token: str, digest: str) -> str | None:
    """The public address of one site's share card: the anonymous route on the deploy's public base,
    carrying the site's own token and the digest of the card's bytes.

    The digest is what makes the address change on a redeploy, so a crawler that cached the old card
    is asking for a URL nothing answers rather than being handed stale pixels. A deploy with no
    `[connect] public_base_url` has no address to name and names none."""
    if not public_base_url:
        return None
    return f"{public_base_url.rstrip('/')}{SITE_CARD_PATH}/{token}/{digest}.{CARD_EXTENSION}"


def site_address(token: str) -> SiteAddress | None:
    """The address a token proves, or None when its signature, surface, or claims do not hold."""
    claims = verify_surface_token(SURFACE_SITES, token)
    if claims is None:
        return None
    portal_embed = claims.get(PORTAL_EMBED_CLAIM)
    if portal_embed not in (None, PORTAL_EMBED_VALUE):
        return None
    try:
        workspace_id = UUID(claims[WORKSPACE_CLAIM])
        conversation_id = UUID(claims[CONVERSATION_CLAIM])
        name = claims[NAME_CLAIM]
    except (KeyError, ValueError):
        return None
    return SiteAddress(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        name=name,
        portal_embed=portal_embed == PORTAL_EMBED_VALUE,
    )


def homepage_embed_url(url: str) -> str:
    """The portal iframe's signed form of one permanent hosted-site URL."""
    prefix, separator, token = url.rpartition("/")
    address = site_address(token)
    if not separator or not prefix.endswith(FRAME_PATH) or address is None:
        raise ValueError("a homepage embed requires a hosted-site URL")
    embed_token = mint_surface_token(
        SURFACE_SITES,
        {
            WORKSPACE_CLAIM: str(address.workspace_id),
            CONVERSATION_CLAIM: str(address.conversation_id),
            NAME_CLAIM: address.name,
            PORTAL_EMBED_CLAIM: PORTAL_EMBED_VALUE,
        },
    )
    return f"{prefix}/{embed_token}"


async def resolve_workspace(request: Request, _auth: SurfaceAuth) -> UUID | Response | None:
    """The workspace the requested site's token names — resolved before any row is read, because a
    public viewer carries no cookie to scope by. A shipped app page's token names its workspace the
    same way, with no row behind it. An unverifiable token answers with the frame's own 404 rather
    than a 401, so a forged address and an unauthorized one are indistinguishable."""
    token = request.path_params.get(TOKEN_PARAM, "")
    address = site_address(token)
    if address is not None:
        return address.workspace_id
    shipped = shipped_address(token)
    return _not_found() if shipped is None else shipped.workspace_id


async def frame(ctx: SurfaceContext, request: Request) -> Response:
    """Open one site: verify, resolve, authenticate, gate, then redirect or embed.

    Whatever trails the token is the site path to open at, so a member can be handed a link to one
    page of a site rather than only its front door — the site's own paths live at the embedded
    origin and are reachable from outside no other way. A deep link proves no more than the bare
    one: both pass the same gate — the agent's visibility for a homepage, the row's own for any
    other site. A bound homepage redirects the portal's iframe to its ingress origin; every other
    site renders its own frame page. A shipped app page's token carries no row and redirects its
    public bundle through `_shipped_frame`."""
    token = request.path_params[TOKEN_PARAM]
    shipped = shipped_address(token)
    if shipped is not None:
        return await _shipped_frame(ctx, request, shipped)
    address = site_address(token)
    if address is None:
        return _not_found()
    site = await _sites(ctx).read(address.conversation_id, address.name)
    if site is None:
        return _not_found()
    frame_path = f"{FRAME_PATH}/{token}"
    base = ctx.public_base_url
    published = site.homepage_agent_id is None and site.visibility == "public"
    card = site.share_card_hash if published else None
    share = _share_tags(
        site.name if published else None,
        f"{base.rstrip('/')}{frame_path}" if base else None,
        _generic_card(ctx, token) if card is None else site_card_url(base, token, card),
        SHARE_CARD_ALT if card is None else SITE_CARD_ALT.format(name=site.name),
    )
    viewer = await _viewer(ctx, request)
    if site.homepage_agent_id is not None:
        if viewer is None:
            return _not_signed_in(ctx, share)
        agent = next((a for a in await ctx.list_agents() if a.id == site.homepage_agent_id), None)
        if agent is None:
            return _not_found()
        if (
            agent.visibility != "workspace"
            and viewer != agent.owner_member_id
            and not await _viewer_is_admin(ctx, viewer)
        ):
            return _not_found()
        if not (address.portal_embed and _is_portal_iframe_request(request)):
            return _into_the_portal(ctx, site.homepage_agent_id, share)
        embedded = ctx.ingress_url(
            site.conversation_id,
            site.port,
            f"/{request.path_params.get(PATH_PARAM, '')}",
            framed_from=_framed_from(request),
        )
        if embedded is None:
            return HTMLResponse(_unconfigured_page(site.name, share))
        return RedirectResponse(embedded, status_code=303)
    if site.visibility != "public" and viewer is None:
        return _not_signed_in(ctx, share)
    if (
        site.visibility == "private"
        and viewer != site.creator_member_id
        and not await _viewer_is_admin(ctx, viewer)
    ):
        return _not_found()
    embedded = ctx.ingress_url(
        site.conversation_id,
        site.port,
        f"/{request.path_params.get(PATH_PARAM, '')}",
        framed_from=_framed_from(request),
    )
    csrf = (
        mint_surface_token(SURFACE_SITES, {CSRF_CLAIM: _session_digest(request)})
        if viewer is not None and viewer == site.creator_member_id
        else ""
    )
    return HTMLResponse(_frame_page(site, embedded, frame_path, csrf, share))


async def _shipped_frame(
    ctx: SurfaceContext, request: Request, shipped: ShippedAddress
) -> Response:
    share = _share_tags(
        None, None, _generic_card(ctx, request.path_params[TOKEN_PARAM]), SHARE_CARD_ALT
    )
    if not _is_portal_iframe_request(request):
        agent = next(
            (
                entry
                for entry in await ctx.list_agents()
                if shipped_app_slug(entry.provisioned_by) == shipped.slug
            ),
            None,
        )
        if agent is None:
            return _not_found()
        return _into_the_portal(ctx, agent.id, share)
    anchor = shipped_anchor(shipped.workspace_id, shipped.slug)
    embedded = ctx.ingress_url(
        anchor,
        serve_port(anchor),
        f"/{request.path_params.get(PATH_PARAM, '')}",
        framed_from=_framed_from(request),
        shipped_slug=shipped.slug,
        shipped_digest=shipped.digest,
    )
    if embedded is None:
        return HTMLResponse(_unconfigured_page(shipped.slug, share))
    return RedirectResponse(embedded, status_code=303)


def _generic_card(ctx: SurfaceContext, token: str) -> str | None:
    return ctx.sites.share_card_url or site_card_url(ctx.public_base_url, token, SHARE_CARD_DIGEST)


def _not_signed_in(ctx: SurfaceContext, share: str) -> HTMLResponse:
    if ctx.sign_in_path is None:
        return HTMLResponse(_page(NOT_PUBLIC_TITLE, _STYLE, NO_SIGN_IN_PAGE, share))
    sign_in = html.escape(ctx.sites.sign_in_url or ctx.sign_in_path, quote=True)
    body = NOT_SIGNED_IN_PAGE.format(sign_in=sign_in)
    return HTMLResponse(_page(NOT_PUBLIC_TITLE, _STYLE, body, share))


def _is_portal_iframe_request(request: Request) -> bool:
    return request.headers.get(FETCH_DESTINATION_HEADER) == IFRAME_DESTINATION


def _framed_from(request: Request) -> str | None:
    if not _is_portal_iframe_request(request):
        return None
    return request.headers.get("referer")


def _into_the_portal(ctx: SurfaceContext, agent_id: UUID, share: str) -> Response:
    portal = ctx.home_url(f"{AGENT_FRAGMENT}{agent_id}")
    if portal is None:
        return HTMLResponse(_page(NO_PORTAL_TITLE, _STYLE, NO_PORTAL_PAGE, share))
    return RedirectResponse(portal, status_code=303)


def _unconfigured_page(title: str, share: str) -> str:
    """What the portal's own frame gets where the deploy hosts no sites: there is no origin to
    embed, so the frame says so rather than framing nothing."""
    return _page(
        html.escape(title),
        _STYLE + _FRAME_STYLE + _HOMEPAGE_FRAME_STYLE,
        f"<main><p>{UNCONFIGURED_BODY}</p></main>",
        share,
    )


async def share_card(ctx: SurfaceContext, request: Request) -> Response:
    """Serve one public site's share card, or the generic card, to whoever asks, and nothing else.

    This is the one anonymous route in the module, because a link unfurler carries no session and
    follows one absolute URL. So it holds five rules of its own:

    **The generic card names nothing.** Its own digest answers with the bundled card for any address
    the token proves, row or no row, since those bytes say nothing about any site.

    **Public only.** The site row is read and its `visibility` decides, here, on this request — not
    at the deploy that drew the card. A level is mutable, and a site that stops being public must
    stop being previewed, so a card written while the site was public answers 404 the moment it is
    not. A homepage-bound site follows its agent and has no level of its own to publish, so it is
    refused outright. Every refusal is the frame's own 404 body, so the route is no oracle for which
    sites exist.

    **No session, no grant.** The URL is public by construction — it goes in a head tag that anyone
    may read — so it must never be a credential. Nothing here reads a cookie, sets one, mints a view
    token, or redirects to a signed artifact link.

    **Cacheable but drainable.** `max-age=600` and not `immutable`: a card must fall out of caches
    within minutes of a site being narrowed, and the digest in the URL already keeps a redeploy from
    being served from a stale cache. A cache in front of this route keeps its copy under this same
    directive, so the window a narrowed site drains in is the one named here.

    **Bytes only.** The key served is the one the row carries. The URL's digest is compared against
    the row's and never used to address anything, so no path in the artifact namespace is reachable
    through here and there is nothing to list."""
    if request.path_params[CARD_HASH_PARAM] == SHARE_CARD_DIGEST:
        return Response(SHARE_CARD, media_type=CARD_MEDIA_TYPE, headers=SITE_CARD_HEADERS)
    site = await _resolve(ctx, request)
    if (
        site is None
        or site.homepage_agent_id is not None
        or site.visibility != "public"
        or site.share_card_blob_key is None
        or site.share_card_hash != request.path_params[CARD_HASH_PARAM]
    ):
        return _not_found()
    return Response(
        await ctx.blob.get(site.share_card_blob_key),
        media_type=CARD_MEDIA_TYPE,
        headers=SITE_CARD_HEADERS,
    )


async def set_visibility(ctx: SurfaceContext, request: Request) -> Response:
    """Move a site between visibility levels for its creator alone. A viewer who is not the creator
    gets the same 404 an unknown site does; a request whose CSRF token is not this session's is
    refused outright, since the creator is proven and the form is not. A homepage-bound site has no
    level of its own to move — its viewers follow the agent — so the post is refused whole."""
    site = await _resolve(ctx, request)
    if site is None:
        return _not_found()
    viewer = await _viewer(ctx, request)
    if viewer is None or viewer != site.creator_member_id:
        return _not_found()
    if site.homepage_agent_id is not None:
        return PlainTextResponse(HOMEPAGE_FOLLOWS_AGENT_BODY, status_code=409)
    form = await request.form()
    if not _csrf_holds(request, str(form.get(CSRF_FIELD, ""))):
        return PlainTextResponse(CSRF_REJECTED_BODY, status_code=403)
    try:
        level = visibility_level(str(form.get(VISIBILITY_FIELD, "")))
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=400)
    await _sites(ctx).set_visibility(site.conversation_id, site.name, level)
    return RedirectResponse(f"{FRAME_PATH}/{request.path_params[TOKEN_PARAM]}", status_code=303)


async def _resolve(ctx: SurfaceContext, request: Request) -> HostedSite | None:
    address = site_address(request.path_params.get(TOKEN_PARAM, ""))
    if address is None:
        return None
    return await _sites(ctx).read(address.conversation_id, address.name)


def _sites(ctx: SurfaceContext) -> HostedSites:
    return HostedSites(ctx.workspace_id, ctx.transaction)


async def _viewer_is_admin(ctx: SurfaceContext, viewer: UUID | None) -> bool:
    if viewer is None:
        return False
    async with ctx.transaction() as connection:
        snapshot = await Seats(ctx.workspace_id).snapshot(connection)
    return any(entry.admin and entry.seated and entry.id == viewer for entry in snapshot.members)


async def _viewer(ctx: SurfaceContext, request: Request) -> UUID | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    email = verify_token(token, ctx.workspace_id)
    if email is None:
        return None
    viewer = await ctx.linked_member(email) or await ctx.link_member(email, email)
    if viewer is None or not await ctx.member_has_access(viewer):
        return None
    return viewer


def _csrf_holds(request: Request, submitted: str) -> bool:
    claims = verify_surface_token(SURFACE_SITES, submitted)
    return claims is not None and claims.get(CSRF_CLAIM) == _session_digest(request)


def _session_digest(request: Request) -> str:
    """Another origin can neither read the HttpOnly cookie nor sign over the deploy secret."""
    return hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()


def _not_found() -> Response:
    return PlainTextResponse(NOT_FOUND_BODY, status_code=404)


def _page(title: str, style: str, body: str, share: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>{share}<style>{style}</style>{body}"
    )


def _share_tags(name: str | None, canonical: str | None, card: str | None, alt: str) -> str:
    """Link crawlers carry no session and republish what they read."""
    title = html.escape(name or GENERIC_SHARE_TITLE, quote=True)
    url = (
        ""
        if canonical is None
        else f'<meta property=og:url content="{html.escape(canonical, quote=True)}">'
    )
    head = (
        "<meta property=og:type content=website>"
        "<meta property=og:site_name content=UFO>"
        f"{url}"
        f'<meta property=og:title content="{title}">'
        f'<meta property=og:description content="{SHARE_DESCRIPTION}">'
    )
    titles = (
        f'<meta name=twitter:title content="{title}">'
        f'<meta name=twitter:description content="{SHARE_DESCRIPTION}">'
    )
    if card is None:
        return head + titles
    image = html.escape(card, quote=True)
    return (
        f"{head}"
        f'<meta property=og:image content="{image}">'
        f"<meta property=og:image:width content={SHARE_CARD_WIDTH}>"
        f"<meta property=og:image:height content={SHARE_CARD_HEIGHT}>"
        f"<meta property=og:image:type content={CARD_MEDIA_TYPE}>"
        f'<meta property=og:image:alt content="{html.escape(alt, quote=True)}">'
        "<meta name=twitter:card content=summary_large_image>"
        f"{titles}"
        f'<meta name=twitter:image content="{image}">'
    )


def _frame_page(
    site: HostedSite,
    embedded: str | None,
    frame_path: str,
    csrf: str,
    share: str,
) -> str:
    """`referrerpolicy` keeps the frame's address (the site token) out of the site's requests;
    `sandbox` withholds `allow-top-navigation` because the framed bytes are model-authored."""
    control = (
        _selector(site.visibility, frame_path, csrf)
        if csrf
        else f"<span class=badge>{VISIBILITY_BADGES[site.visibility]}</span>"
    )
    site_view = (
        f'<iframe src="{html.escape(embedded, quote=True)}" '
        f'title="{html.escape(site.name, quote=True)}" referrerpolicy=no-referrer '
        f'sandbox="{IFRAME_SANDBOX}" allow="fullscreen"></iframe>' + FOCUS_REFRESH_SCRIPT
        if embedded is not None
        else f"<main><p>{UNCONFIGURED_BODY}</p></main>"
    )
    return _page(
        html.escape(site.name),
        _STYLE + _FRAME_STYLE,
        f"<header><span class=name>{html.escape(site.name)}</span>{control}</header>{site_view}",
        share,
    )


def _selector(current: Visibility, frame_path: str, csrf: str) -> str:
    options = "".join(
        f"<option value={level}{' selected' if level == current else ''}>{label}</option>"
        for level, label in VISIBILITY_LABELS.items()
    )
    return (
        f'<form method=post action="{html.escape(frame_path, quote=True)}/visibility">'
        f'<input type=hidden name={CSRF_FIELD} value="{html.escape(csrf, quote=True)}">'
        f"<select name={VISIBILITY_FIELD}>{options}</select>"
        "<button type=submit>Save</button></form>"
    )


_STYLE = (
    ":root{color-scheme:light dark}"
    "*{box-sizing:border-box}"
    "body{margin:0;font:15px/1.5 system-ui,sans-serif;display:flex;flex-direction:column;"
    "height:100vh;background:Canvas;color:CanvasText}"
    "main{padding:24px;max-width:60ch}"
    "a{color:LinkText}"
)
_FRAME_STYLE = (
    "header{display:flex;align-items:center;gap:12px;padding:8px 12px;"
    "border-bottom:1px solid color-mix(in srgb, CanvasText 15%, transparent)}"
    ".name{font-weight:600}"
    ".badge,form{margin-left:auto;font-size:13px;opacity:.75}"
    "form{display:flex;gap:6px;opacity:1}"
    "select,button{font:inherit;font-size:13px;padding:2px 6px}"
    "iframe{flex:1;width:100%;border:0}"
)
_HOMEPAGE_FRAME_STYLE = "body{background:transparent}"

SITES_SURFACE = SurfaceSpec(
    name=SURFACE_SITES,
    routes=(
        SurfaceRoute(
            method="GET",
            path=f"{SITE_CARD_SEGMENT}/{{{TOKEN_PARAM}}}/{{{CARD_HASH_PARAM}}}.{CARD_EXTENSION}",
            handler=share_card,
        ),
        SurfaceRoute(method="GET", path=f"{{{TOKEN_PARAM}}}", handler=frame),
        SurfaceRoute(method="POST", path=f"{{{TOKEN_PARAM}}}/visibility", handler=set_visibility),
        SurfaceRoute(method="GET", path=f"{{{TOKEN_PARAM}}}/{{{PATH_PARAM}:path}}", handler=frame),
    ),
    identify=resolve_workspace,
)
