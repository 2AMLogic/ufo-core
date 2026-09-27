"""The default index over the test database, scoped to whichever workspace the caller binds."""

from ufo_ext_index_default import DefaultIndex

from ufo.db import workspace_tx
from ufo.runtime.workspace import ws_current


def default_index() -> DefaultIndex:
    return DefaultIndex(transaction=workspace_tx, workspace=lambda: ws_current().workspace_id)
