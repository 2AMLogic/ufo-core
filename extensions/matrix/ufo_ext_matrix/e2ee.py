"""The `matrix-e2ee` extra: what it installs, and what an encrypted room meets without it.

Olm and Megolm come from `vodozemac`, a Rust extension module, and the crypto store is sealed with
`cryptography`, which builds on OpenSSL. This distribution ships the `pulse` skill pack beside the
Matrix surface from one dependency list, so neither library is baseline: an install for `pulse`
alone downloads no native crypto. `pip install "ufo-integrations[matrix-e2ee]"` adds both.

Without the extra every module here imports and an unencrypted room is served as ever. An encrypted
one raises `ExtraMissing`, whose message is the one sentence that names the extra and the command
that installs it; the listener warns `matrix.crypto_extra_missing` against the batch that carried
ciphertext and reads the rest of it. Neither carries key material, and neither ends a stream."""

EXTRA = "matrix-e2ee"
DISTRIBUTION = "ufo-integrations"
INSTALL = f'pip install "{DISTRIBUTION}[{EXTRA}]"'


class CryptoUnavailable(Exception):
    """A message is bound for an encrypted room and the bot has no device keys to encrypt it
    with."""


class ExtraMissing(CryptoUnavailable):
    """An encrypted room was met and the libraries that read one are not installed."""

    def __init__(self) -> None:
        super().__init__(f"an encrypted Matrix room needs the {EXTRA} extra: {INSTALL}")
