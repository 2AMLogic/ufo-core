---
name: matrix-setup
description: "Load when an admin connects this workspace to Matrix, or when a connected bot hears nothing in its rooms. Not for setting up another chat surface, and not for sending a message into a room — a connected bot answers its rooms on its own."
metadata:
  depends: []
---
# Connect Matrix, in chat

Connecting is three things in order: the bot's own account on a homeserver, the credential slots, and
the connect action on the `matrix` surface. None of it is a setting a member edits; each step is a
turn in this conversation.

## The bot needs an account of its own

The workspace's agent syncs as one Matrix user, and that user is the installation the surface routes
by. Register a fresh account for it on the homeserver and take that account's access token. An account
another bot or a person already syncs as is the one setup mistake with no visible symptom — both
readers receive the same stream, and each treats the other's messages as its own.

## Ask for the slots, never for the token itself

| Slot | Holds | Needed for |
| --- | --- | --- |
| `matrix_homeserver` | The homeserver's base URL, e.g. `https://matrix.example.org` | Every room |
| `matrix_access_token` | The bot account's access token | Every room |
| `matrix_store_key` | At least 32 random characters, e.g. from `openssl rand -base64 32` | An encrypted room |

Call `request_credentials` for them. The admin fills them where the transcript cannot see them, so
neither the token nor the store key is ever typed into the conversation or repeated back.

`matrix_store_key` seals the bot's encryption keys at rest, and it is the one slot with a value the
admin keeps: the keys sealed under it are unreadable under any other value, so a store key that
changes strands the device that was using it.

## Connect, and read what the homeserver says

With the slots filled, take the connect action on the `matrix` surface row. It asks the homeserver
whose token it holds and binds that MXID as the workspace's installation, so the bot the token belongs
to is the bot that gets bound and a wrong token is a refusal rather than a wrong binding. A second
token for the same bot replaces the binding; the listener reads the binding each sync round, so a
connect takes effect without a restart.

```
admin: connect our Matrix bot
agent: Connected @ufo:example.org. It is listening.
```

A bot another workspace already holds is refused by name: that MXID belongs elsewhere, and which
workspace holds it is not this workspace's to know. Register a separate account for this workspace's
agent and connect that one.

## Then invite it

A connected bot is in no rooms. A workspace member invites it to a room and it joins itself; an
invitation from anyone the workspace cannot resolve to a member is left standing. In a room it has
joined, the bot answers when it is mentioned, when the room holds only it and one other person, and
when a room it is already in conversation with wants it.

## Traps

Every trap here presents the same way — the agent does not answer — so read them in order rather than
reasoning back from the silence.

- **The deploy's pack does not bundle `matrix`.** A `[pack] name = "assistant"` in the deploy's
  configuration narrows the active set to that pack's own extensions, and `matrix` is not among them,
  so the surface never activates and no listener runs. A throwaway local pack listing the assistant
  set and `matrix` is the way through. A lockfile pinning the set reaches it only with `[pack] name`
  unset, because the pack narrows the active set after the lockfile has filled it.
- **The room is encrypted and `matrix_store_key` is empty.** An encrypted room is read and answered
  as any other — but only with that slot filled. Empty, the bot has no device keys, logs
  `matrix.crypto_no_keys` against the first batch that carries ciphertext, hears nothing it can read
  there, and refuses to answer rather than answering in the clear. Fill the slot; the bot reads what
  is sent after that, not what was sent before it had keys.
- **The deploy has no `matrix-e2ee` extra.** The libraries that carry Olm and Megolm are an extra, so
  a deploy installed without it serves unencrypted rooms and logs `matrix.crypto_extra_missing`
  against the first encrypted event it meets. `pip install "ufo-integrations[matrix-e2ee]"` on the
  deploy is the way through, and the store key alone does not substitute for it.
- **The store key changed.** The keys sealed under the old value no longer open, the bot logs
  `matrix.crypto_store_locked`, and its encrypted rooms go quiet. Restore the old value, or issue the
  bot a new token — a new device — and let it start a fresh store.
- **The bot's token is in a second client.** The device is the one the token is bound to, so another
  client holding that token publishes its own keys over the bot's and members stop being able to read
  it. One token, one client.
- **Two readers share one account.** A token reused from another agent's bot makes both sync as the
  same user, and each reads the other's messages as its own. One account per workspace's agent.
- **The bot was never invited.** Joining a room is a member's act, not the bot's. A room nobody invited
  it to sends it nothing, and an invitation from someone the workspace cannot resolve to a member is
  left standing rather than refused.
