"""Unit tests for the provider adapters, the registry, and the facades.

No network. Nothing here touches the database.

Three things are covered, and each corresponds to a way the refactor that
created this package could have gone wrong:

1. **The facades still resolve.** Several modules import ``ProbeResult``,
   ``probe_models`` and the reason codes from ``model_discovery_service``, and
   ``ai_credentials_service`` imports ``SDK_CREDENTIAL_COMPATIBILITY`` from
   ``environment_service`` rather than from ``sdk_constants`` where it is
   declared. Those re-exports are invisible in a grep of the declaring module's
   consumers, so an import that breaks breaks at runtime in an unrelated domain.

2. **The key-prefix rule gives the same answers it always did**, and gives a
   safe answer for providers it was never asked about — which is what let five
   call sites drop their ``type == ANTHROPIC`` guards.

3. **The derived mappings still equal the tables they replaced.** Each of the
   literal values below was the incumbent table's value; this is the differential
   test that the collapse did not change any of them.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.credentials.ai_credential import AICredentialData, AICredentialType
from app.services.ai_providers import registry


# ---------------------------------------------------------------------------
# 1. Facades
# ---------------------------------------------------------------------------

class TestFacadesStillResolve:
    def test_model_discovery_service_reexports(self):
        """The names five modules and two test files import from here."""
        from app.services.credentials import model_discovery_service as mds

        assert mds.ProbeResult is not None
        assert mds.ERROR_INVALID_KEY == "invalid_key"
        assert mds.OAUTH_TOKEN_UNSUPPORTED == "oauth_token_unsupported"
        assert "no_list_endpoint" in mds.SKIP_REASONS
        assert "no_base_url" in mds.SKIP_REASONS
        assert callable(mds.probe_models)

    def test_probe_result_identity_is_shared(self):
        """The re-exported ``ProbeResult`` is the adapters' class, not a copy.

        Two classes with the same name would make ``isinstance`` checks fail in
        one direction only — the kind of bug that shows up in one caller.
        """
        from app.services.ai_providers.base import ProbeResult as declared
        from app.services.credentials.model_discovery_service import (
            ProbeResult as reexported,
        )

        assert declared is reexported

    def test_environment_service_reexport_of_sdk_compatibility(self):
        """``ai_credentials_service`` imports this from ``environment_service``."""
        from app.services.environments.environment_service import (
            CREDENTIAL_TYPE_TO_BAG_KEY,
            SDK_CREDENTIAL_COMPATIBILITY,
            apply_credential_to_bag,
            make_empty_credential_bag,
        )

        assert SDK_CREDENTIAL_COMPATIBILITY["claude-code"] == ["anthropic", "minimax"]
        assert callable(apply_credential_to_bag)
        assert callable(make_empty_credential_bag)
        assert CREDENTIAL_TYPE_TO_BAG_KEY

    def test_detect_anthropic_credential_type_still_importable(self):
        from app.utils import detect_anthropic_credential_type

        assert callable(detect_anthropic_credential_type)


# ---------------------------------------------------------------------------
# 2. classify_key
# ---------------------------------------------------------------------------

class TestClassifyKey:
    @pytest.mark.parametrize(
        "api_key,is_oauth,env_var,label",
        [
            ("sk-ant-oat01-abc", True, "CLAUDE_CODE_OAUTH_TOKEN", "OAuth Token"),
            ("sk-ant-api03-xyz", False, "ANTHROPIC_API_KEY", "API Key"),
            ("", False, "ANTHROPIC_API_KEY", "API Key (Empty)"),
            (None, False, "ANTHROPIC_API_KEY", "API Key (Empty)"),
            ("something-else", False, "ANTHROPIC_API_KEY", "API Key (Unknown Format)"),
        ],
    )
    def test_anthropic_classification_matrix(self, api_key, is_oauth, env_var, label):
        """The four outcomes the incumbent prefix rule produced, unchanged."""
        result = registry.get_adapter(AICredentialType.ANTHROPIC).classify_key(api_key)
        assert result.is_oauth_token is is_oauth
        assert result.env_var_name == env_var
        assert result.label == label

    def test_utils_facade_agrees_with_the_adapter(self):
        """``detect_anthropic_credential_type`` returns exactly what it always did."""
        from app.utils import detect_anthropic_credential_type

        assert detect_anthropic_credential_type("sk-ant-oat01-abc") == (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OAuth Token",
        )
        assert detect_anthropic_credential_type("sk-ant-api03-xyz") == (
            "ANTHROPIC_API_KEY",
            "API Key",
        )
        assert detect_anthropic_credential_type("") == (
            "ANTHROPIC_API_KEY",
            "API Key (Empty)",
        )
        assert detect_anthropic_credential_type("weird") == (
            "ANTHROPIC_API_KEY",
            "API Key (Unknown Format)",
        )

    @pytest.mark.parametrize(
        "cred_type",
        [t for t in AICredentialType if t is not AICredentialType.ANTHROPIC],
    )
    def test_non_anthropic_providers_never_report_an_oauth_token(self, cred_type):
        """Even handed an Anthropic-looking string.

        This is the property that lets callers ask any adapter without first
        checking the credential's type. If a provider ever answered True here,
        every dropped ``type == ANTHROPIC`` guard would become a behaviour change.
        """
        adapter = registry.get_adapter(cred_type)
        assert adapter.classify_key("sk-ant-oat01-abc").is_oauth_token is False
        assert adapter.classify_key("whatever").is_oauth_token is False
        assert adapter.classify_key(None).is_oauth_token is False

    def test_issues_oauth_tokens_agrees_with_classify_key(self):
        """The flag is a shortcut past a decrypt, so it must not lie.

        A provider flagged ``issues_oauth_tokens=False`` is never decrypted by
        the two call sites that consult it, so if its ``classify_key`` could in
        fact return True for some key, those sites would silently stop enforcing
        the OAuth guard for it.
        """
        oauth_looking = "sk-ant-oat01-abc"
        for cred_type in AICredentialType:
            adapter = registry.get_adapter(cred_type)
            can_answer_true = adapter.classify_key(oauth_looking).is_oauth_token
            if can_answer_true:
                assert adapter.issues_oauth_tokens is True, (
                    f"{type(adapter).__name__}.classify_key can return "
                    f"is_oauth_token=True but issues_oauth_tokens is False — the "
                    f"callers that check the flag first would skip the guard"
                )

    def test_only_anthropic_issues_oauth_tokens(self):
        for cred_type in AICredentialType:
            adapter = registry.get_adapter(cred_type)
            expected = cred_type is AICredentialType.ANTHROPIC
            assert adapter.issues_oauth_tokens is expected

    def test_non_anthropic_providers_declare_no_key_kind_env_var(self):
        """Only Anthropic routes its key into a different env var by key kind."""
        for cred_type in AICredentialType:
            adapter = registry.get_adapter(cred_type)
            classification = adapter.classify_key("some-key")
            if cred_type is AICredentialType.ANTHROPIC:
                assert classification.env_var_name == "ANTHROPIC_API_KEY"
            else:
                assert classification.env_var_name is None


# ---------------------------------------------------------------------------
# 3. The derived mappings equal the tables they replaced
# ---------------------------------------------------------------------------

# Every value below was read off the incumbent table before it was deleted.
_EXPECTED_SDK_ENGINE = {
    AICredentialType.ANTHROPIC: "claude-code/anthropic",
    AICredentialType.MINIMAX: "claude-code/minimax",
    AICredentialType.OPENAI: "opencode/openai",
    AICredentialType.GOOGLE: "opencode/google",
    AICredentialType.OPENAI_COMPATIBLE: "opencode/openai_compatible",
}
_EXPECTED_ACCOUNT_CONFIG_DISPLAY = {
    AICredentialType.ANTHROPIC: ("Claude", "claude"),
    AICredentialType.OPENAI: ("OpenAI", "openai"),
    AICredentialType.GOOGLE: ("Gemini", "gemini"),
    AICredentialType.OPENAI_COMPATIBLE: ("", "openai-compatible"),
    AICredentialType.MINIMAX: ("MiniMax", "minimax"),
}
_EXPECTED_CATALOG_ENGINE_PROVIDER = {
    AICredentialType.ANTHROPIC: ("claude-code", "anthropic"),
    AICredentialType.MINIMAX: ("claude-code", "minimax"),
    AICredentialType.OPENAI: ("opencode", "openai"),
    AICredentialType.GOOGLE: ("opencode", "google"),
    AICredentialType.OPENAI_COMPATIBLE: ("opencode", "openai_compatible"),
}
_EXPECTED_BAG_KEY = {
    AICredentialType.ANTHROPIC: "anthropic_api_key",
    AICredentialType.MINIMAX: "minimax_api_key",
    AICredentialType.OPENAI_COMPATIBLE: "openai_compatible_api_key",
    AICredentialType.OPENAI: "openai_api_key",
    AICredentialType.GOOGLE: "google_api_key",
}


class TestAbsorbedTablesAreUnchanged:
    @pytest.mark.parametrize("cred_type", list(AICredentialType))
    def test_sdk_engine(self, cred_type):
        assert registry.get_adapter(cred_type).sdk_engine == _EXPECTED_SDK_ENGINE[
            cred_type
        ]

    @pytest.mark.parametrize("cred_type", list(AICredentialType))
    def test_account_config_display(self, cred_type):
        adapter = registry.get_adapter(cred_type)
        assert (
            adapter.account_config_display_name,
            adapter.account_config_slug,
        ) == _EXPECTED_ACCOUNT_CONFIG_DISPLAY[cred_type]

    @pytest.mark.parametrize("cred_type", list(AICredentialType))
    def test_catalog_engine_provider(self, cred_type):
        assert registry.get_adapter(
            cred_type
        ).catalog_engine_provider == _EXPECTED_CATALOG_ENGINE_PROVIDER[cred_type]

    @pytest.mark.parametrize("cred_type", list(AICredentialType))
    def test_bag_key(self, cred_type):
        assert registry.get_adapter(cred_type).bag_key_api_key == _EXPECTED_BAG_KEY[
            cred_type
        ]

    def test_empty_bag_has_exactly_the_incumbent_keys_in_order(self):
        """The bag's key set and order are load-bearing for anything comparing bags."""
        from app.services.environments.sdk_constants import make_empty_credential_bag

        assert list(make_empty_credential_bag()) == [
            "anthropic_api_key",
            "minimax_api_key",
            "openai_compatible_api_key",
            "openai_compatible_base_url",
            "openai_compatible_model",
            "openai_api_key",
            "google_api_key",
            "model_default_conversation",
            "model_default_building",
        ]

    def test_openai_compatible_is_the_only_multi_slot_provider(self):
        """Its base_url/model slots are the reason ``apply_to_bag`` is not one line."""
        from app.services.environments.sdk_constants import (
            apply_credential_to_bag,
            make_empty_credential_bag,
        )

        bag = make_empty_credential_bag()
        apply_credential_to_bag(
            bag,
            AICredentialType.OPENAI_COMPATIBLE,
            AICredentialData(api_key="k", base_url="https://e", model="m"),
        )
        assert {k: v for k, v in bag.items() if v is not None} == {
            "openai_compatible_api_key": "k",
            "openai_compatible_base_url": "https://e",
            "openai_compatible_model": "m",
        }

    def test_apply_to_bag_ignores_a_type_no_adapter_serves(self):
        """Matching the previous if/elif chain, which had no else branch."""
        from app.services.environments.sdk_constants import (
            apply_credential_to_bag,
            make_empty_credential_bag,
        )

        bag = make_empty_credential_bag()
        apply_credential_to_bag(bag, "not-a-provider", AICredentialData(api_key="k"))
        assert all(value is None for value in bag.values())

    def test_only_openai_compatible_requires_extra_fields(self):
        """The server-side authority behind ``_validate_credential_data``'s 400s."""
        for cred_type in AICredentialType:
            adapter = registry.get_adapter(cred_type)
            expected = cred_type is AICredentialType.OPENAI_COMPATIBLE
            assert adapter.requires_base_url is expected
            assert adapter.requires_model is expected

    def test_only_minimax_publishes_no_model_list_endpoint(self):
        """What ``model_health_service`` used to spell as ``!= MINIMAX``."""
        for cred_type in AICredentialType:
            adapter = registry.get_adapter(cred_type)
            expected = cred_type is not AICredentialType.MINIMAX
            assert adapter.supports_model_listing is expected


# ---------------------------------------------------------------------------
# Registry lookup semantics
# ---------------------------------------------------------------------------

class TestRegistryLookup:
    def test_string_type_resolves(self):
        """Rows load ``type`` as a plain string on some paths."""
        assert registry.get_adapter("anthropic").type is AICredentialType.ANTHROPIC

    def test_unknown_type_is_none_not_an_exception_for_find(self):
        assert registry.find_adapter("not-a-provider") is None
        assert registry.find_adapter(None) is None

    def test_unknown_type_raises_for_get(self):
        with pytest.raises(registry.UnknownProviderError):
            registry.get_adapter("not-a-provider")

    def test_a_bare_list_of_models_is_accepted(self):
        """The one tolerance that is deliberate."""
        from app.services.ai_providers._http import ids_from_openai_shape

        assert ids_from_openai_shape([{"id": "a"}, {"id": "b"}]) == ["a", "b"]

    def test_entries_that_are_not_usable_are_skipped_not_fatal(self):
        """The second deliberate tolerance: a bad *entry* inside a good envelope."""
        from app.services.ai_providers._http import ids_from_openai_shape

        payload = {"data": [{"id": "a"}, "nonsense", {"no_id": 1}, {"id": "b"}]}
        assert ids_from_openai_shape(payload) == ["a", "b"]

    @pytest.mark.parametrize("payload", ["a string", 42, None, True])
    def test_a_payload_that_is_neither_object_nor_array_raises(self, payload):
        """An empty list here is not a neutral value — it is a *success*.

        ``refresh_credential_models`` treats an empty result as a clean listing:
        it overwrites ``discovered_models`` with ``[]`` and clears
        ``models_discovery_error``. ``model_health`` then reads the wiped cache
        as "empty but present", skips both its discovery branch and its
        unverified guard, and reports OK. So a garbled response returning ``[]``
        would silently destroy a good model list, erase the record that anything
        went wrong, and turn an UNVERIFIED verdict into a healthy one.

        Raising records the exception class name against the credential and
        leaves the previous list alone, which is what the four per-provider
        listers this replaced always did.
        """
        from app.services.ai_providers._http import ids_from_openai_shape

        with pytest.raises(TypeError):
            ids_from_openai_shape(payload)

    def test_probe_models_reports_an_unknown_type_as_a_benign_skip(self):
        """A column value the enum no longer has must not 500 the discovery cron."""
        from app.services.credentials.model_discovery_service import probe_models

        result = asyncio.run(probe_models("not-a-provider", "key"))
        assert result.ok is True
        assert result.reason == "unsupported_type"
        assert result.is_skip is True

    def test_override_is_restored_after_an_exception(self):
        """A failing test must not leak a stub into the next one."""
        from tests.utils.ai_provider import stub_all_providers

        original = registry.get_adapter(AICredentialType.OPENAI)
        with pytest.raises(RuntimeError):
            with stub_all_providers():
                raise RuntimeError("boom")
        assert registry.get_adapter(AICredentialType.OPENAI) is original

    def test_override_rejects_an_adapter_whose_type_does_not_resolve(self):
        """A silently-ignored override is a stub that never intercepts anything."""
        from app.services.ai_providers.base import BaseProviderAdapter

        class Nonsense(BaseProviderAdapter):
            type = "not-a-provider"

        with pytest.raises(registry.UnknownProviderError):
            with registry.override_for_tests(Nonsense()):
                pass


class TestStubbingKeepsEveryProviderFact:
    """The stub replaces the network call and nothing else.

    An adapter is not only an I/O boundary — every per-provider fact now lives on
    it, and ``make_empty_credential_bag`` / ``apply_credential_to_bag`` read those
    at call time. A stub that substituted its own data for all five providers
    would reshape the credential bag inside the block, dropping slots and writing
    one provider's key into another's, with nothing raising. These tests pin that
    it does not.
    """

    def test_the_credential_bag_is_unchanged_under_the_stub(self):
        from app.services.environments.sdk_constants import make_empty_credential_bag
        from tests.utils.ai_provider import stub_all_providers

        expected = list(make_empty_credential_bag())
        with stub_all_providers():
            assert list(make_empty_credential_bag()) == expected

    def test_a_key_lands_in_its_own_slot_under_the_stub(self):
        from app.services.environments.sdk_constants import (
            apply_credential_to_bag,
            make_empty_credential_bag,
        )
        from tests.utils.ai_provider import stub_all_providers

        with stub_all_providers():
            bag = make_empty_credential_bag()
            apply_credential_to_bag(
                bag, AICredentialType.OPENAI, AICredentialData(api_key="sk-openai")
            )
            assert {k: v for k, v in bag.items() if v is not None} == {
                "openai_api_key": "sk-openai"
            }

    def test_provider_facts_are_unchanged_under_the_stub(self):
        from tests.utils.ai_provider import stub_all_providers

        before = {
            t: (
                registry.get_adapter(t).sdk_engine,
                registry.get_adapter(t).account_config_display_name,
                registry.get_adapter(t).account_config_slug,
                registry.get_adapter(t).bag_keys,
                registry.get_adapter(t).requires_base_url,
                registry.get_adapter(t).issues_oauth_tokens,
            )
            for t in AICredentialType
        }
        with stub_all_providers():
            after = {
                t: (
                    registry.get_adapter(t).sdk_engine,
                    registry.get_adapter(t).account_config_display_name,
                    registry.get_adapter(t).account_config_slug,
                    registry.get_adapter(t).bag_keys,
                    registry.get_adapter(t).requires_base_url,
                    registry.get_adapter(t).issues_oauth_tokens,
                )
                for t in AICredentialType
            }
        assert after == before

    def test_classify_key_is_delegated_not_stubbed(self):
        from tests.utils.ai_provider import stub_all_providers

        with stub_all_providers():
            anthropic = registry.get_adapter(AICredentialType.ANTHROPIC)
            assert anthropic.classify_key("sk-ant-oat01-x").is_oauth_token is True
            openai = registry.get_adapter(AICredentialType.OPENAI)
            assert openai.classify_key("sk-ant-oat01-x").is_oauth_token is False

    def test_the_recorder_reports_which_provider_was_probed(self):
        from app.services.credentials.model_discovery_service import probe_models
        from tests.utils.ai_provider import probe_success, stub_all_providers

        with stub_all_providers(probe_success(["m"])) as recorder:
            asyncio.run(probe_models(AICredentialType.GOOGLE, "k"))
            asyncio.run(probe_models(AICredentialType.OPENAI, "k"))
        recorder.assert_probed(times=2)
        assert recorder.probed_providers == [
            AICredentialType.GOOGLE,
            AICredentialType.OPENAI,
        ]

    def test_a_stub_never_claims_minting(self):
        """Inheriting a provisioner would inherit a live provider client."""
        from tests.utils.ai_provider import stub_all_providers

        with stub_all_providers():
            assert not any(a.supports_minting for a in registry.all_adapters())
