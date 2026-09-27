from dataclasses import dataclass
from uuid import UUID

from ufo.sdk.operator import OperatorGrant, OperatorLookup

OPERATOR_RULE = "sample_fleet"
OPERATOR_DOMAIN = "operator.test"
OPERATOR_SIGN_IN = "/sample/operator-sign-in"


@dataclass(frozen=True)
class SampleOperatorRule:
    """The probe rule the `operator_rules` point registers: an operator's workspace is any whose
    first member is at `OPERATOR_DOMAIN`, a seated admin there reaches the whole fleet and a
    seated admin anywhere else reaches their own workspace, and `?ws=` names a workspace by id or
    by domain. Every read goes through core's lookup, so a test driving a surface under this rule
    proves the calls core makes and the reach it enforces."""

    lookup: OperatorLookup
    sign_in: str | None = OPERATOR_SIGN_IN

    async def admit(self, claimed_ws: UUID, email: str) -> OperatorGrant | None:
        if not await self.lookup.seated_admin(claimed_ws, email):
            return None
        return OperatorGrant(
            home=claimed_ws, reach="fleet" if await self.operator_workspace(claimed_ws) else "own"
        )

    async def select(self, grant: OperatorGrant, target: str) -> UUID | None:
        try:
            return UUID(target)
        except ValueError:
            return await self.lookup.workspace_by_domain(target)

    async def operator_workspace(self, workspace_id: UUID) -> bool:
        return await self.lookup.workspace_domain(workspace_id) == OPERATOR_DOMAIN
