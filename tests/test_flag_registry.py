"""
Tests for flag_registry.py - both its own internal integrity (no key
collisions, every flag has real data) and that it stays honest about
matching the actual gating code in handlers.py/subscription.py. This
second category is the whole point of having a registry at all: if
someone changes has_feature("clickability") to has_feature("pro_names")
inside _validate_clickability_flag without updating the registry, THIS
file should catch it, not a confused user reading a wrong /help screen.
"""
import re

import flag_registry as fr


class TestRegistryStructuralIntegrity:
    def test_every_flag_has_a_description(self):
        for key, flag in fr.FLAGS.items():
            assert flag["description"], f"{key} has an empty description"

    def test_every_flag_used_in_at_least_one_real_command(self):
        for key, flag in fr.FLAGS.items():
            assert flag["used_in"], f"{key} isn't used by any command"
            for cmd in flag["used_in"]:
                assert cmd in fr.COMMANDS, f"{key} references unknown command {cmd!r}"

    def test_limit_event_and_limit_feature_never_collide(self):
        """The exact scenario this registry was built to handle safely:
        -limit means something different in /newevent than in
        /updatefeature - two separate registry entries, never surfaced
        to the wrong command."""
        newevent_keys = [k for k, _ in fr.flags_of("newevent")]
        updatefeature_keys = [k for k, _ in fr.flags_of("updatefeature")]
        assert "limit_event" in newevent_keys
        assert "limit_feature" not in newevent_keys
        assert "limit_feature" in updatefeature_keys
        assert "limit_event" not in updatefeature_keys

    def test_clickability_shared_across_three_commands_is_one_entry(self):
        """-clc is genuinely identical in meaning across all 3 commands
        it appears on - unlike -limit, it's correctly ONE registry
        entry referenced by all three via used_in, not duplicated."""
        assert fr.FLAGS["clickability"]["used_in"] == ["newevent", "editevent", "shareevent"]
        assert len([k for k in fr.FLAGS if k == "clickability"]) == 1


class TestRegistryMatchesRealGatingCode:
    """Cross-checks gated_by_feature against the actual has_feature()
    calls inside the real validators - this is the drift-detection
    layer the registry exists to enable."""

    def test_clickability_gating_matches_validator(self):
        with open("handlers.py") as f:
            src = f.read()
        # _validate_clickability_flag's own has_feature call
        match = re.search(r'_validate_clickability_flag.*?has_feature\(chat_id, "(\w+)"\)', src, re.DOTALL)
        assert match, "Could not find _validate_clickability_flag's has_feature call"
        assert fr.FLAGS["clickability"]["gated_by_feature"] == match.group(1)

    def test_waitlist_gating_matches_validator(self):
        with open("handlers.py") as f:
            src = f.read()
        match = re.search(r'_validate_waitlist_visibility_flag.*?has_feature\(chat_id, "(\w+)"\)', src, re.DOTALL)
        assert match, "Could not find _validate_waitlist_visibility_flag's has_feature call"
        assert fr.FLAGS["waitlist"]["gated_by_feature"] == match.group(1)

    def test_limit_event_gating_matches_validator(self):
        with open("handlers.py") as f:
            src = f.read()
        match = re.search(r'_validate_limit_flag.*?has_feature\(chat_id, "(\w+)"\)', src, re.DOTALL)
        assert match, "Could not find _validate_limit_flag's has_feature call"
        assert fr.FLAGS["limit_event"]["gated_by_feature"] == match.group(1)

    def test_notgoinglist_is_genuinely_ungated_in_code(self):
        """-ngl must have NO has_feature gate anywhere near its own
        parsing/application - if someone adds one without updating the
        registry, this test won't catch that directly, but it does
        confirm the registry's current 'ungated' claim isn't obviously
        contradicted by an existing validator function for it."""
        with open("handlers.py") as f:
            src = f.read()
        assert "_validate_notgoinglist_flag" not in src  # no such gate function exists
        assert fr.FLAGS["notgoinglist"]["gated_by_feature"] is None


class TestRenderFlagsDetailOutput:
    def test_render_includes_every_flag_for_a_command(self):
        text = fr.render_flags_detail("newevent")
        for key, flag in fr.flags_of("newevent"):
            short_name = flag["names"][0].lstrip("-")
            assert f"\\-{short_name}" in text

    def test_render_marks_gated_flags_with_tier_notice(self):
        text = fr.render_flags_detail("newevent")
        lines = text.split("\n")
        clc_line = next(l for l in lines if l.startswith("\\-clc"))
        ngl_line = next(l for l in lines if l.startswith("\\-ngl"))
        assert "Requires a higher tier" in clc_line
        assert "Requires a higher tier" not in ngl_line

    def test_shareevent_flags_come_before_shared_clickability(self):
        """Matches the original hand-written text's convention:
        command-specific flags first, the shared/override flag last."""
        text = fr.render_flags_detail("shareevent")
        mgl_pos = text.index("\\-mgl")
        clc_pos = text.index("\\-clc")
        assert mgl_pos < clc_pos

    def test_unknown_command_returns_empty_string(self):
        assert fr.render_flags_detail("not_a_real_command") == ""


class TestDefaultValuePrefixInRenderedOutput:
    """Flags with a real default value show "(default: value)" right
    before the description; flags with no default (default=None,
    purely optional) don't get this prefix at all."""

    def test_flag_with_default_shows_prefix(self):
        text = fr.render_flags_detail("shareevent")
        assert "\\(default: onlycount\\) Going list visibility" in text
        assert "\\(default: on\\) whether names in the post" in text

    def test_flag_without_default_has_no_prefix(self):
        text = fr.render_flags_detail("newevent")
        # -date has no default - must not show a stray "(default: ...)"
        for line in text.split("\n"):
            if "\\-date" in line:
                assert "default:" not in line


class TestGoingIconsHaveRealDefaults:
    """Real gap found: -gi/-goingicon and -ni/-notgoingicon DO have
    fixed fallback values in the code (config.py's DEFAULT_GOING_ICON/
    DEFAULT_NOTGOING_ICON, used via "g_icon if g_icon else
    DEFAULT_GOING_ICON" in handlers.py), but the registry incorrectly
    said default=None, meaning /help never showed this real default."""

    def test_goingicon_shows_real_default(self):
        text = fr.render_flags_detail("newevent")
        assert "\\(default: 👍\\) Custom Going icon" in text

    def test_notgoingicon_shows_real_default(self):
        text = fr.render_flags_detail("newevent")
        assert "\\(default: ❌\\) Custom Not Going icon" in text

    def test_defaults_match_config_constants(self):
        """The registry's hardcoded defaults must never drift from the
        actual fallback values used in code."""
        import config
        assert fr.FLAGS["goingicon"]["default"] == config.DEFAULT_GOING_ICON
        assert fr.FLAGS["notgoingicon"]["default"] == config.DEFAULT_NOTGOING_ICON


class TestNoFixedDefaultFlagsExplainWhy:
    """Flags with no real default value (default=None) now explain WHY
    in their own description, rather than silently saying nothing about
    what happens when omitted - covers 3 genuinely distinct reasons:
    absence-of-feature (-date/-limit), action-not-setting (-a/-p),
    partial-update (-minlevel), presence-only-filter (-pro), and
    dynamic-inheritance (-sngl/-swl, already covered by their own
    pre-existing "inherits" wording)."""

    def test_date_explains_absence(self):
        text = fr.render_flags_detail("newevent")
        assert "no date shown at all" in text

    def test_limit_event_explains_no_cap(self):
        text = fr.render_flags_detail("newevent")
        assert "no cap applies at all" in text

    def test_active_passive_explain_action_not_setting(self):
        text = fr.render_flags_detail("updateuser")
        assert text.count("this is an action, not a persistent setting") == 2

    def test_minlevel_explains_partial_update(self):
        text = fr.render_flags_detail("updatefeature")
        assert "existing tier requirement is simply left unchanged" in text

    def test_pro_filter_explains_presence_only(self):
        text = fr.render_flags_detail("allgroups")
        assert "presence\\-only filter" in text

    def test_sngl_swl_already_explain_inheritance(self):
        """Pre-existing wording, confirmed still present and correct -
        these have NO fixed default because behavior genuinely varies
        per event (inherits the event's own -ngl/-wl), not because of
        an oversight."""
        text = fr.render_flags_detail("shareevent")
        assert "inherits the event's own \\-ngl setting" in text
        assert "inherits the event's own \\-wl setting" in text
