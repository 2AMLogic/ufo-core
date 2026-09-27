from types import SimpleNamespace
from uuid import uuid4

import pytest
from ufo_ext_sample.manifest import manifest as sample_manifest
from ufo_ext_sample.operator import OPERATOR_RULE, OPERATOR_SIGN_IN

import ufo.host.ext.loader as loader
from ufo.config import DEFAULT_OPERATOR_RULE
from ufo.host.ext.loader import FIRST_PARTY_ENV, discovered
from ufo.runtime.ext.manifest import Manifest
from ufo.runtime.ext.operator import (
    SEATED_ADMIN,
    FleetDirectory,
    FleetReachRequired,
    OperatorGrant,
    OperatorRuleSpec,
    installed_operator,
    select_operator_rule,
)


def test_boot_builds_the_rule_its_config_names_and_leaves_the_rest_inert() -> None:
    sample = sample_manifest()

    assert select_operator_rule(DEFAULT_OPERATOR_RULE, (sample,)).sign_in is None
    assert select_operator_rule(OPERATOR_RULE, (sample,)).sign_in == OPERATOR_SIGN_IN


def test_a_rule_name_nobody_registers_or_two_register_fails_boot() -> None:
    sample = sample_manifest()
    twin = Manifest(name="twin", version="0", operator_rules=sample.operator_rules)
    shadow = Manifest(name="shadow", version="0", operator_rules=(SEATED_ADMIN,))

    with pytest.raises(ValueError, match="'fleet' names no registered rule"):
        select_operator_rule("fleet", (sample,))
    with pytest.raises(ValueError, match=f"{OPERATOR_RULE!r} is registered more than once"):
        select_operator_rule(DEFAULT_OPERATOR_RULE, (sample, twin))
    with pytest.raises(ValueError, match=f"{DEFAULT_OPERATOR_RULE!r} is registered more than once"):
        select_operator_rule(DEFAULT_OPERATOR_RULE, (shadow,))


def test_an_operator_rule_is_refused_unless_its_distribution_is_first_party(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = (OperatorRuleSpec(name="acme", build=SEATED_ADMIN.build),)
    acme = SimpleNamespace(
        dist=SimpleNamespace(name="acme"),
        module="acme_ext",
        load=lambda: lambda: Manifest(name="acme", version="0", operator_rules=rules),
    )
    monkeypatch.setattr(loader, "entry_points", lambda group: (acme,))
    monkeypatch.delenv(FIRST_PARTY_ENV, raising=False)

    with pytest.raises(ValueError, match="cannot declare privileged capabilities"):
        discovered()
    monkeypatch.setenv(FIRST_PARTY_ENV, "acme")
    assert set(discovered()) == {"acme"}


async def test_the_fleet_directory_refuses_a_grant_of_one_workspace() -> None:
    with pytest.raises(FleetReachRequired):
        await FleetDirectory().read(OperatorGrant(home=uuid4(), reach="own"))


def test_reading_the_operator_before_boot_installs_one_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="no operator rule is installed"):
        installed_operator()
