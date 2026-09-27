"""Public re-export: seat state, the validated writes over it, the `member_added` point
`add_member` calls, the address shape rule, the signup-subject derivation, and the member reads —
the rules stay core's, the extension decides when to apply them.

`ufo.sdk` is a package of thin re-export modules with an empty `__init__.py`, so the public
surface lives in named modules like this one."""

from ufo.runtime.seats import (
    MemberAdded as MemberAdded,
)
from ufo.runtime.seats import (
    MemberAddedSpec as MemberAddedSpec,
)
from ufo.runtime.seats import (
    SeatEntry as SeatEntry,
)
from ufo.runtime.seats import (
    Seats as Seats,
)
from ufo.runtime.seats import (
    email_domain as email_domain,
)
from ufo.runtime.seats import (
    has_spoken as has_spoken,
)
from ufo.runtime.seats import (
    member_by_email as member_by_email,
)
from ufo.runtime.seats import (
    member_is_admin as member_is_admin,
)
from ufo.runtime.seats import (
    signup_workspace_id as signup_workspace_id,
)
from ufo.runtime.seats import (
    workspace_domain as workspace_domain,
)
from ufo.runtime.seats import (
    workspace_subject as workspace_subject,
)
