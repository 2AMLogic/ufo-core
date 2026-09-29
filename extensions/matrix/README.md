# ufo-ext-matrix

A Matrix chat surface for [ufo](https://github.com/ufo-ai/ufo-core): a room is a conversation, and
the people in it are members.

The surface is durable, like Slack's. A bot user reads its rooms over the client-server `/sync`
stream, and a turn answers in the room it came from: its reply as rich text under the message it
answers, the files it shared as messages of their own, and the words it marks before it ends as they
are marked. While it works, the room shows it working; when it ends in a question, the room answers
by typing a label or tapping a poll.

## What it adds

A room is mostly not addressed to the agent, so the surface decides per message whether it founds a
turn:

| Message | Turn |
| --- | --- |
| From a sender who resolves to no member | None, and no reply — unless it is a code proving that sender's MXID |
| Addressed to the bot, or sent in a direct room (the bot and one other) | Founded |
| Unaddressed, in a room that already holds a conversation | Founded only if `ambient_reply_wanted` says the agent is wanted |
| Unaddressed, in a room with no conversation yet | None |
| The bot's own echo, an `m.notice`, an edit (`m.replace`), a redaction, a redacted event | None |
| An encrypted event the bot holds no key for | None until its key arrives, then as above |

A message is addressed to the bot four ways, and each names the bot whole, so a mention of `@ufobot`
addresses nobody named `@ufo`:

| Addressed by | Read from |
| --- | --- |
| An intentional mention | `m.mentions.user_ids` |
| A pill a client rendered instead | `https://matrix.to/#/<bot mxid>` in `formatted_body` |
| The bot's MXID or display name in the words | `body`, whatever its case |
| A reply to something the bot said | `m.in_reply_to` naming a message of the bot's |

Unaddressed lines the agent did not take part in ride the next admitted message as room context, and
the recent 20 are the evidence the ambient decision reads.

| Matrix | ufo |
| --- | --- |
| Bot MXID | The installation, bound to one workspace |
| Room id | The conversation's key |
| Sender MXID | A member: linked on first contact when its homeserver is the workspace's own domain and `localpart@domain` is a member's email, or linked by a code the member proved it with; otherwise nobody |
| Room where everyone but the bot is a member | A room audience: the room's memory and the workspace's shared memory |
| Room with anyone else in it, or in a workspace with no domain | A foreign audience: the room's memory alone, nothing internal |
| Terminal turn | `PUT /rooms/{roomId}/send/m.room.message/ufo-{turn_id}` |
| The message that founded the turn | `m.in_reply_to`, or `m.thread` under its root where the member spoke in a thread |
| Markdown the turn wrote | `formatted_body` in `org.matrix.custom.html`, beside the words in `body` |
| A shared file | `POST /_matrix/media/v3/upload`, then `m.image` / `m.video` / `m.audio` / `m.file` under the reply |
| A file a member shares | The bytes fetched, unsealed where the room is encrypted, stored, and delivered to `uploads/` in the conversation's workspace, where the turn that answers them reads it |
| A detailed write-up | A link in the reply to the portal, never an upload |
| Words marked mid-turn | One message each, under `ufo-say-{reply_id}` |
| A terminal `ask_user` | The words, then its questions as a labelled list in a message of their own under `ufo-question-{turn_id}`, and an `m.poll.start` where one question takes one choice |
| A reply of labels, or a tap on the poll | The options it names, admitted under that event's id |
| The answer that landed | An `m.replace` of the question's own message, marking what was chosen, under `ufo-answered-{event_id}` |
| A turn that is running | `POST .../receipt/m.read/{eventId}`, then `PUT .../typing/{userId}` refreshed until the turn ends |

A direct room is a room like any other, so a room's audience only ever narrows: once anyone who is
not a member joins, the room is foreign for good, whoever leaves after.

The transaction id is the turn's, so a retried delivery is the same transaction and the homeserver
answers it with the event it already sent. Each file is sent under `ufo-file-{turn_id}-{artifact_id}`
and each mid-turn reply under `ufo-say-{reply_id}`, so a delivery recovered after a crash re-sends
none of them. A turn whose whole answer is silence sends nothing. A question is written out with
numbered options, apart from the words, and `matrix_ext_asking` records which message carries it; a
connect or credential handoff points at the workspace, since a room carries neither. A cancelled turn posts the reason core gave — an archived conversation, a removed seat — or,
with none, that it was stopped.

A whole event weighs at most 65536 bytes and a reply carries its words twice, so a reply over 4096
bytes of Markdown is written in parts, cut between paragraphs and never leaving a code fence open.
The first part's event id is the reference core records, and every part relates to the same message.

Which room message a turn answers is stored per turn in `matrix_ext_answering`: a writeback names the
turn, the room, and the member, so admission records the event id every later message of that turn
relates to. An answer is a message like any other, so the turn it founds answers the answer, and its
reply lands under that.

The typing indicator runs off `tail(turn_id)` and stops on the turn's terminal or parked frame. The
`PUT` carries the homeserver's own timeout and is refreshed under it, so a stream that drops leaves
the indicator to the server's clock rather than leaving a room typing.

A file a member shares is fetched and stored under an artifact key, and the workspace path it lands
under is `uploads/<name>`, where the turn that answers the member reads it. A file from an encrypted
room is unsealed first, and its ciphertext is checked against the file's own sha256 before anything
is decrypted, so bytes the media repository substituted are refused rather than opened.

Its caption is what the member said. MSC2530 puts the caption in `body` and the name in `filename`,
so a file sent with a sentence founds a turn carrying that sentence, and a bare file carries its
name. Where the file landed is recorded in `matrix_ext_delivered`, keyed on the event that carried
it, so a batch replayed after a crash delivers it once.

The `/sync` position is stored per workspace in `matrix_ext_since`, a table the extension's
migration owns, after each batch is delivered. A restart resumes from it; a batch replayed after a
crash is admitted once, because each message's event id is its admission key. The first sync of a
stream only fixes where it stands — history from before the bot listened founds nothing.

A room that saw more than 50 events between two syncs comes back `limited`. The surface pages its
history back from the gap to where the stream stood, 100 messages a page for at most 5 pages, and
hears what it finds ahead of the timeline. A message that fails to admit is logged by error class and
skipped, and one bot's failure backs that bot off without stopping the others.

## Homeserver

The bot lives on a homeserver, and the `matrix_homeserver` slot is the only thing that names which.
Three answers are in use, and they differ in one place: whom the homeserver vouches for.

| Mode | Who runs it | A member's MXID | Becomes a member by |
| --- | --- | --- | --- |
| Own | The deploy, one homeserver behind one workspace | `@bob:acme.example`, the workspace's own domain | First contact |
| Shared | One homeserver behind many workspaces | `@bob:ufo.example`, a domain no workspace owns | A code |
| Elsewhere | Somebody else: a public homeserver, or an organisation's existing one | `@bob:matrix.org` | A code |

Which mode a deploy is in is decided by the homeserver's `server_name`, never by the URL
`matrix_homeserver` holds. The two routinely differ — this repository's own encryption job runs a
homeserver named `integration.test` at `http://127.0.0.1:8008` — and it is the name that ends up
after the colon in every MXID the server issues. A deploy standing up its own homeserver sets
`server_name` to the workspace's domain; a homeserver reached at `https://matrix.acme.example` and
named `acme.example` is own mode, and the same host named anything else is not, however it is
reached and whoever runs it.

An MXID whose server name is the workspace's own domain is a member on first contact, because the
homeserver serving that domain vouches for its users the way a mail server does for addresses. Every
other MXID is nobody until the member proves it with a code. Own mode is the mode that rule was
written for; the other two never reach it, and pay one code exchange per member instead.

Which means the mailbox trap below is own mode's to answer, and only own mode's. A deploy that runs
its own homeserver decides who registers on it and can hold its usernames to mail names. A homeserver
shared with other ufo users is one whose registrations somebody else decides — so a workspace there
takes a domain of its own, or none, and never the shared server's name.

A workspace with no domain of its own vouches for nobody, whichever homeserver it uses.

Federation is what a member elsewhere crosses. A room reaches the bot only where the two homeservers
federate, so a homeserver closed to federation serves the accounts it hosts and no others, in every
mode.

## Linking a Matrix ID

A member whose MXID lives on another homeserver — a public one, say — links it by proving they hold
it. The member asks in chat, on any surface; `matrix_link_account` records a claim on the MXID and
answers with a one-time code, and the member sends that code to the bot from the MXID, in a direct
room with the bot alone. The bot never writes first.

| Step | What happens |
| --- | --- |
| Claim | A code of 8 characters, live for 15 minutes, stored as a salted slow hash; a new claim on the same MXID replaces the old code |
| Invite | While a claim is live, the bot joins a room the claimed MXID invites it to; with none, the invitation is left standing |
| Proof | The right code from the claimed MXID links it to the member who claimed it; the bot answers in the room, and the message founds no turn |
| Wrong code | Answered with the tries left; the fifth wrong code voids the claim |
| Expired | Answered once, and the claim is gone |
| Next message | Founds a turn as that member |

A code in a room with anyone else in it, or from any other MXID, proves nothing and is not answered.
The proving message's event id is kept, so a batch replayed after a crash does not admit it.

An admin unlinks an MXID with `matrix_unlink_account`. From then on it speaks for nobody — a
domain-matched MXID included — until someone proves it again with a code. Claims and unlinks live in
`matrix_ext_claim` and `matrix_ext_link`, the extension's own tables; the link itself is core's.

## How a room answers a question

A room has no buttons, so a question is a numbered list, and a reply that is nothing but labels is the
answer. Anything else is words — which is how a member answers a question that asked for words, and
how they say something that merely starts with a number.

| Ask | Labels | Answered by |
| --- | --- | --- |
| One question | `1`, `2` | `2`, or `1, 3` where the question takes several |
| Up to four questions | `1a`, `2b` | `1a 2b`, one label per question |

A question that takes one choice keeps the first label its reply names. `answerable_question` is the
only gate on who may answer what: a question already answered, one past the ask's own range, and one
put to another member are each refused there, and the reply is admitted as the words it is.

## Encryption

The bot reads and writes end-to-end encrypted rooms as one device: the one its access token is
bound to, which `/account/whoami` names. A restart keeps that device, so members' clients see the
same device they already know, not a new one on every boot. The libraries that carry it ship in the
`matrix-e2ee` extra, so a deploy that wants them asks for them.

| Direction | What happens |
| --- | --- |
| Inbound | Room keys arrive as Olm to-device events and are read before the timeline. Each `m.room.encrypted` event is decrypted into the event it carries and then heard exactly as a plain one: member gate, audience, ambient decision, admission. |
| Key not yet here | The event is parked, its key is asked for with `m.room_key_request`, and it is heard once the key lands. After 10 minutes it is dropped with a warning. Ciphertext is never heard. |
| Undecryptable | An event whose plaintext does not read as a Matrix event is dropped with `matrix.undecryptable`, named by error class, and the stream carries on past it. |
| Outbound | A post into a room with `m.room.encryption` state is Megolm-encrypted. The session key goes first over Olm to every device of every joined member that lacks it. |
| Rotation | A room's session is replaced when the room's rotation settings say it has served long enough (100 messages or 7 days by default), or when a device it was shared with has left. |
| Trust | First use. The keys a device id first shows are pinned; a device that later shows other keys is sent no room key and not believed. |
| Verification | A member's client starts an `m.key.verification.*` exchange and the bot answers it to its end, recording that device verified beside its pin. It gates nothing: first use is what decides who is sent a room key. |
| No device keys | A batch carrying ciphertext to a bot with no device logs `matrix.crypto_no_keys` once and is heard whole apart from that ciphertext — the tell for a `matrix_store_key` slot left empty, which the device open itself passes over in silence. |

The device's account, its Olm and Megolm sessions, the pinned device keys, and the parked events
live in `matrix_ext_crypto`, a table the extension's migration owns — never in workspace files, a
scoped store, or the sandbox. Every row is sealed with AES-256-GCM under a key derived from the
`matrix_store_key` slot, bound to the row's address, and named by an HMAC, so the table shows only
the bot's own user and device id. The account row is minted once per device by an insert that the
table's primary key arbitrates, so two processes opening the device together agree on one account
and publish one set of keys. A token bound to another device finds no rows, starts a new device, and
logs `matrix.crypto_device_new`.

### Verifying the bot's device

A member verifies the bot from the bot's device entry in their own client, and the whole exchange is
to-device protocol: no command, no keyword, nothing typed in a room. The bot answers, and it never
starts one.

| Event | The bot's answer |
| --- | --- |
| `m.key.verification.request` | `.ready` naming `m.sas.v1` as its one method, or `.cancel` with `m.unknown_method` |
| `m.key.verification.start` | `.accept` carrying the hash of a fresh ephemeral key and the `.start` as it arrived, so the key is committed to before the member's is known |
| `m.key.verification.key` | Its own `.key`, the secret agreed from both |
| `m.key.verification.mac` | Its own `.mac` and a `.done`, and the device recorded verified — or `.cancel` with `m.key_mismatch`, and nothing recorded |

The exchange runs over Olm, where the sending device proves its identity key. The opening request is
answered in the clear as well, since several clients send it that way and it names no key; anything
later in the clear is refused with `m.invalid_message`, because the homeserver stamps the sender of
an unencrypted to-device event, so it names a user and not the device the verification is about.

An exchange in flight lives in the process's memory: a `vodozemac.Sas` holds an ephemeral key it
neither pickles nor gives back, so there is nothing to seal into a row. A restart mid-exchange is one
the member starts again from a prompt they are still looking at, and an exchange abandoned for
10 minutes is let go — the age is read at every event of it, so an exchange that old is answered no
further whether or not another one has started in the meantime. Each workspace holds 32 exchanges at
once and lets go of its own oldest past that, so a member opening exchange after exchange crowds out
their workspace's and no other's.

Cross-signing is not here. The bot holds no master key, signs no device but its own, and reads no
user's cross-signing keys, so the MAC it checks is the device key it pinned and a `.mac` naming any
other key ends the exchange.

### The `matrix-e2ee` extra

`vodozemac` is a Rust extension module and `cryptography` builds on OpenSSL, and this distribution
ships `pulse` alongside `matrix`, so neither is a baseline dependency: a `pip install
ufo-integrations` for the skill packs alone pulls no native crypto. `pip install
"ufo-integrations[matrix-e2ee]"` adds them. Without the extra the extension imports and serves
unencrypted rooms as ever; an encrypted room is met with one sentence naming the extra —
`matrix.crypto_extra_missing` on the listener, and a refusal to post rather than a post in the clear.

Olm and Megolm come from [vodozemac](https://github.com/matrix-org/vodozemac), the Matrix.org
Foundation's Rust implementation, through the PyPI package `vodozemac`, whose binding is
[matrix-nio/vodozemac-python](https://github.com/matrix-nio/vodozemac-python) — the matrix-nio
project's work, and where the trust sits for what holds the bot's long-lived keys:

| Candidate | Why not, or why |
| --- | --- |
| `python-olm` | Wraps libolm, which the Matrix.org Foundation no longer maintains and has replaced with vodozemac. No macOS arm64 wheel. |
| `mautrix` with its encryption extra | Builds on `python-olm`, and brings its own client, state store, and `aiohttp`. |
| `matrix-nio[e2e]` | The same binding under it, so the crypto is the same choice; it keeps its state in its own SQLite file through `peewee`, outside the extension's tables, and brings a second HTTP client. |
| `vodozemac` | The primitives alone, so the store and the wire stay this extension's. Around a hundred wheels at 0.10 — CPython 3.10 through 3.15 and PyPy 3.11, manylinux and musllinux across x86_64, aarch64, armv7l, ppc64le, s390x and i686, both macOS architectures, Windows amd64 and arm64 — and an sdist behind them. |

The key exchange around the primitives — key upload, query and claim, to-device handling, room-key
sharing — is `crypto.py`, and the store is `crypto_store.py`. Sealing uses `cryptography`, at the
`>=49` floor ufo itself holds.

## Install

```bash
pip install "ufo-integrations[matrix-e2ee] @ git+https://github.com/2AMLogic/ufo-integrations"
```

Drop `[matrix-e2ee]` for a deploy whose rooms are unencrypted, and the native crypto libraries stay
out of the environment.

Installing the distribution registers the `ufo.extension` entry point. Activating this surface is a
separate act: a deploy runs the extensions its active set names, and `ufoctl init` narrows that set
to the assistant pack's, which does not bundle `matrix`. A deploy reaches this surface by naming
`matrix` in an active set of its own — [the root README](../../README.md#install) states the
narrowing, and [`pulse`](../pulse/README.md#install) carries the pack files such a set is built from.

| Setting | Where | Holds |
| --- | --- | --- |
| `UFO_MATRIX_BOTS` | Deploy environment | The bot MXIDs this deploy's listener runs, comma-separated |
| `matrix_homeserver` | Workspace credential slot | The homeserver's base URL, e.g. `https://matrix.example.org` |
| `matrix_access_token` | Workspace credential slot | The bot user's access token |
| `matrix_store_key` | Workspace credential slot | At least 32 random characters sealing the bot's encryption keys, e.g. `openssl rand -base64 32` |

## Connect

Setup happens in chat. `matrix_connect` is an instance action bound to the `matrix` surface, offered
on that one surface row as `action:surface:matrix_connect`, and the `matrix-setup` skill carries the
order of the steps.

| Step | In chat |
| --- | --- |
| The two slots | The agent calls core's `request_credentials` for `matrix_homeserver` and `matrix_access_token`, so the token is filled where the transcript cannot see it |
| Connect | `GET /_matrix/client/v3/account/whoami` names the bot the token belongs to, and that MXID is bound as the workspace's installation |
| Invite | A workspace member invites the bot to a room and it joins itself, as does an MXID a member is proving; an invitation from anyone else is left standing |

**A deploy is not connected until `matrix_connect` has run in chat.** Filled credential slots and a
bot named in `UFO_MATRIX_BOTS` are the deploy's half; the binding is the workspace's, and the
listener admits nothing for an installation it does not hold. An invitation sent before the binding
exists is left standing rather than refused, so a bot that looks configured sits in no room and says
nothing about it — invite again once connected, and it joins within a sync round.

That order is forced rather than chosen: the slots are filled by an action offered in chat, so the
chat needs the booted deploy and the binding needs the chat.

**The bot's homeserver must be the workspace's own domain** for a member to be linked on first
contact, because that is the rule above — an MXID is a member when its homeserver is that domain and
`localpart@domain` is a member's email. A homeserver whose `server_name` is anything else can link
nobody that way, so every sender is nobody, no turn is founded, and the room stays silent under a
deploy that is otherwise correct. A member on another homeserver links by proving a code instead.

The bot the token belongs to is the bot that gets bound, so a wrong token is a refusal and never a
wrong binding. A second token for the same bot replaces the binding, and the listener reads the
binding each sync round, so a connect takes effect without a restart. A bot another workspace already
holds is refused by name — that MXID belongs elsewhere, and which workspace holds it is not this
workspace's to know.

## Traps

- **A reply of labels is an answer, and one label with a word is not.** `1` answers; `1 more thing`
  is words. A member who means the first option and says so in a sentence is heard as a sentence,
  which is the reading that never puts words the agent invented in their mouth.
- **A question put to one member does not silence the others.** Another member's `1` is admitted as
  the words it is, the question stays open, and only the member it names answers it.
- **Only the question is rewritten.** The mark lands on the message `post` recorded as the turn's
  question, whatever the answer replied to — the words the question came with, or any older line of
  the bot's, stay as the room read them. A question too long for one event is answered and left
  unmarked, since rewriting its first part would strand the rest.
- **A poll carries one question and one choice.** An ask of several questions, or one that takes
  several answers, reaches the room as the labelled list alone.
- **Typing is not delivery.** The hub is lossy, so a frame that never arrives costs the room an
  indicator and never the turn's answer — and a reporter the stream cancels leaves the indicator to
  the homeserver's timeout rather than sending a last stop.
- **An unlisted bot is bound but deaf.** The listener runs only the MXIDs `UFO_MATRIX_BOTS` names;
  binding one the deploy does not list admits nothing until it does.
- **`ufoctl ext install matrix` pins from a catalog this deploy may not run.** It reads an extension
  store, and `ufoctl init` writes no `[ext].store` into `ufo.toml`, so the command answers
  `extension store not enabled` and pins nothing. A deploy without a store names `matrix` in its own
  active set instead.
- **An empty store key is a bot with no device keys.** Without `matrix_store_key` the bot hears
  nothing it can read in an encrypted room, and a reply into one is refused rather than sent in
  the clear.
- **A deploy without the `matrix-e2ee` extra reads no encrypted room.** Unencrypted rooms are served
  as ever; an encrypted one logs `matrix.crypto_extra_missing` naming the extra, and a reply into one
  is refused with the same sentence. The store key does not substitute for the extra, nor the extra
  for the store key.
- **A file the bot sends is not read back.** In an encrypted room the bytes are sealed before they
  are uploaded, so the media repository holds ciphertext and the key travels inside the Megolm
  payload — the timeline names no url that opens it.
- **A member's filename is a name, never a path.** `content.filename` is the sender's to choose, so
  it reaches a workspace path through core's own sanitiser: components dropped, everything outside a
  word character collapsed, a dots-only or empty name replaced, and a name already in `uploads/`
  numbered rather than written over. The set it is numbered against is the directory read at
  delivery, not a set carried in memory, so two members sharing `chart.png` an hour apart each keep
  their own file.
- **A replayed file event lands once, and the gap is narrow rather than closed.** The `/sync`
  position is written after the batch is delivered, so a crash between the two replays it with the
  same event ids. `matrix_ext_delivered` keys each delivery on the event that carried it: a replayed
  event answers from that row, so the member keeps one copy and the turn one artifact row. The row is
  written after core accepts the delivery, so a crash inside that one gap still replays as it did
  before.
- **An unconfigured deploy boots, and answers nothing.** `requires` is empty because matrix consumes
  no seam another extension serves — a credential slot is not one of the four seams `requires` can
  name. The deploy-refuses-to-boot instinct is `deploy_keys`, already carried here for
  `MATRIX_BOTS`; the three credential slots are per-workspace values instead, filled by
  `matrix_connect` in chat.
- **An event that will not decrypt is a dropped event.** A plaintext that does not read as a Matrix
  event is logged as `matrix.undecryptable` with its error class and skipped. It is never retried
  into a stalled stream, so one malformed sender cannot silence a bot.
- **A changed store key strands the device.** The store no longer opens, the bot logs
  `matrix.crypto_store_locked`, and encrypted rooms go quiet. Restore the old value, or issue a new
  token — a new device — to start a fresh store.
- **The bot's device is the bot's alone.** Use its token in no other client: a client that shares the
  device id publishes its own keys over the bot's, and peers stop reading the bot.
- **Trust is first use, verified or not.** The bot believes the keys a device first shows, and a
  device that completes SAS is recorded beside its pin without being sent anything a pinned device
  is not. Verification is what the member gets out of it: their client stops warning about the
  bot's device.
- **The member's client starts, and the bot waits.** The bot answers a request with `.ready` and
  sends no `.start` of its own, so a client that asks and then waits to be started sits there until
  its own timeout. The spec lets either end start; this end never does.
- **A verification asked for in a room is not read.** The to-device exchange is the one the bot
  answers. An `m.key.verification.request` sent as a room message is an `m.room.message` of a
  msgtype that founds no turn, so it is heard as nothing at all.
- **The bot compares no emoji.** It has no screen, so the emoji its side of an exchange would show
  is never displayed and never checked. What the bot answers is the member's own confirmation,
  which their client sends as a MAC; a homeserver that relayed the exchange with keys of its own
  would be caught by the member's screen, and the bot's silence is not a second screen. The
  guarantee is that the member's client verified the device keys the homeserver published for the
  bot, over a secret agreed in the exchange — and no more than that.
- **Cross-signing is not here.** No master key, no device signed but the bot's own, no user's
  cross-signing keys read. A member whose client insists on a cross-signed identity sees the bot's
  device verified and the bot's user unverified, which is the whole of what the bot claims.
- **An event older than its key's reach stays unread.** A parked event whose key does not arrive
  within 10 minutes is dropped with `matrix.undecryptable_dropped`. The bot asks the sender for the
  key once; whether the sender's client answers is its own policy.
- **A new token is a new transaction scope.** Transaction ids are idempotent per access token, so a
  reply retried across a token rotation can land twice.
- **A file is best effort.** One upload the homeserver refuses is logged by error class and its
  siblings still land, so a room can hold three of four files and the reply that named all four.
- **An orphaned upload is the cost of at-least-once.** Recovery repeats `attach`, so bytes can reach
  the media repository twice; the second `mxc://` is unreferenced and the repository keeps it.
- **A write-up needs a portal.** The detailed report is a link into the deploy's portal, and a deploy
  without one, or a room the portal shows nobody, points at the workspace instead — the reply never
  carries an empty body for a write-up the room cannot link.
- **Only a member brings the bot into a room.** An invitation from anyone who resolves to no member —
  a stranger on the bot's own homeserver included — is left standing, unless a member's claim on
  that MXID is live.
- **An MXID links to one member for good.** Core records the first member an MXID is linked to and
  offers no call that moves it. An admin's unlink silences the MXID, but proving it again links it
  back to that same member only; a claim by anyone else is refused.
- **A claim opens the door to its MXID.** While it is live, the bot joins a room that MXID invites
  it to, whoever else is in the room. The room founds nothing until the MXID is proved, and a room
  with others in it is foreign.
- **A non-member is not answered.** A sender outside the workspace's domain, or on it with no member
  behind `localpart@domain`, founds no turn and gets no reply, however directly it addresses the bot.
  Nothing is spent on the line; it is still heard as room context for the members. The one exception
  is a code sent in a direct room for a claim on that sender's MXID.
- **A homeserver username is taken to be a mailbox.** First contact links `@alice:example.com` to
  the member `alice@example.com`, so the homeserver must hand out usernames only to the people who
  hold those mailboxes. Disable open registration on it, and keep its usernames equal to mail names —
  anyone who can register `@alice` before Alice does speaks as her. A homeserver shared with other ufo
  users is one whose registrations somebody else decides, so no workspace on it takes that server's
  name as its own domain.
- **A room of more than 50 others is foreign, and stays foreign.** Every joined user is resolved
  to a member before a room reads internal memory, and past 50 the surface does not ask. A room's
  audience only narrows: one that went foreign — past 50, or through a non-member who has since
  left — never reads internal memory again, however small the room gets.
- **Backfill can hear a room's pre-join past.** A room the bot joined inside a sync window can come
  back `limited`; its history pages then reach back to the stored position, and with a `shared`
  history visibility they hold messages sent before the join. A member's pre-join line that
  mentions the bot, or any line in a direct room, can found a turn. The surface accepts this
  deliberately: nothing before the stored position is ever read, and only a room joined within
  the window exposes pre-join lines.
- **A long gap is heard in part.** Past the 500 messages its history pages reach, a gap after a long
  outage is heard from its newest end; anything earlier founds nothing.

## Tests

```bash
pytest extensions/matrix
```

`test_matrix_contracts.py` needs only `pytest`: the import gate, which events may found a turn, who a
line addresses, the labels a question is answered by, and the HTML, splitting, and relations the
surface writes. The rest need `ufo` and skip without it.

| Module | What it drives |
| --- | --- |
| `test_matrix_surface.py` | The listener and the three delivery handlers against a fake homeserver |
| `test_matrix_linking.py` | A claim through its proof, the same way |
| `test_matrix_without_the_extra.py` | What a deploy without `matrix-e2ee` meets, by switching the flag its absence sets |
| `test_matrix_crypto.py` | Real Olm and Megolm between the bot and members' devices, and a real SAS exchange driven by a fake client that spells the spec's commitment and MACs out for itself, through that same fake homeserver with key and to-device endpoints added. The one module that also skips without the extra |
| `test_matrix_registry.py` | The installed entry point through ufo's loader, and the migrations applied |

Two modules drive a real homeserver, and one env var names it for both: they are collected only
where `MATRIX_INTEGRATION_HOMESERVER` holds a homeserver's base url. The jobs that run the whole
suite never set it, so what they collect is unchanged and the registry job's no-skip rule holds.
Both register their own throwaway users, so the named homeserver must allow registration; a private
Synapse container does.

| Module | What it drives |
| --- | --- |
| `test_matrix_integration.py` | The delivery handlers: a reply, a thread, files, a re-handed say, and a foreign token |
| `test_matrix_crypto_integration.py` | An encrypted round trip with a second account, and what only a homeserver answers for: which batch a room key arrives in, the count of a pool it drew on, the fallback key it reaches for, and the `device_lists` it emits |

The delivery module needs `ufo` installed, and the crypto module needs the `matrix-e2ee` extra
beside it. A container to run them against, and the run itself:

```bash
mkdir -p /tmp/synapse && cd /tmp/synapse
docker run --rm -v /tmp/synapse:/data -e SYNAPSE_SERVER_NAME=integration.test \
  -e SYNAPSE_REPORT_STATS=no ghcr.io/element-hq/synapse:latest generate
# Registration is off in what `generate` writes, and the rate limits refuse a suite that registers
# two accounts a test. The file it wrote ends without a newline, so the first line appended is blank.
docker run --rm --user root --entrypoint /bin/sh -v /tmp/synapse:/data \
  ghcr.io/element-hq/synapse:latest -c 'printf "\nenable_registration: true\n\
enable_registration_without_verification: true\nrc_registration:\n  per_second: 1000\n\
  burst_count: 1000\nrc_message:\n  per_second: 1000\n  burst_count: 1000\n" >> /data/homeserver.yaml'
docker run -d --name synapse -p 8017:8008 -v /tmp/synapse:/data ghcr.io/element-hq/synapse:latest
```

```bash
MATRIX_INTEGRATION_HOMESERVER=http://localhost:8017 \
  uv run --python 3.12 --extra matrix-e2ee --with pytest --with pytest-xdist \
  --with "ufo @ file:///home/ubuntu/ufo-core" \
  pytest extensions/matrix/tests/test_matrix_integration.py \
  extensions/matrix/tests/test_matrix_crypto_integration.py -v
```

The `encryption against a Synapse container` job stands that container up for the crypto module and
nothing else, and holds itself to having run: a report with a skip in it, or with no test in it,
fails the job rather than passing as a suite that tested nothing. `rc_login` is raised there too,
since the `device_lists` test logs a member in a second time for their second device.

## License

Apache-2.0.
