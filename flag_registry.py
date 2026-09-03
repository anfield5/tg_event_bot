"""
flag_registry.py - static, code-only source of truth for every command
and every flag this bot supports, plus a small renderer that generates
/help detail text from it.

This is DELIBERATELY NOT a database table and NOT runtime-editable via
any bot command. It lives in the codebase, changes through git+deploy
like any other code, and exists purely to stop the same flag
description from being hand-copied into multiple places in
help_system.py (which is exactly how -newevent's flag text and
/shareevent's flag text drifted out of sync with each other more than
once earlier in this project's history).

What this DOES give you:
  - One place to edit a flag's description/default/gating - every
    /help screen that shows it picks up the change automatically.
  - A single point where "which flags does command X have" is defined,
    so a test can assert help text and command behavior agree.

What this deliberately does NOT do:
  - It does not change what a flag DOES. -clc's actual on/off logic
    still lives in event_engine.py/_mention_link - this registry only
    knows -clc's spelling, default, gating, and one-line description.
  - It is not a mechanism for reconfiguring which feature gates which
    flag at runtime. gated_by_feature here must always match the real
    has_feature() call inside that flag's own validator in handlers.py/
    subscription.py - if you change one, you must change the other by
    hand (a test can catch drift, see tests/test_flag_registry.py).

On the -limit collision specifically: -limit means something
completely different in /newevent (event headcount cap) than in
/updatefeature (per-tier usage cap). These are two SEPARATE registry
entries below (keys "limit_event" and "limit_feature") - the registry
is keyed by a semantic id, not by the literal flag string, so both can
freely declare names=["-limit"] without colliding. Each is only ever
looked up via its own command's `used_in` membership, so a caller
asking "what are /newevent's flags" can never accidentally surface
/updatefeature's -limit or vice versa.
"""

# ---------------------------------------------------------------------------
# Flag definitions
# ---------------------------------------------------------------------------
# Each entry:
#   names            - all accepted spellings, short form first
#   values           - the syntax shown for the flag's argument, or None
#                       for a bare flag with no value (none currently, but
#                       kept for completeness)
#   default           - the value used if the flag is omitted, or None if
#                       there's no fixed default (e.g. -d has no fallback)
#   description       - one or more lines explaining what it does; this is
#                       the ONLY place this text is written
#   gated_by_feature  - the feature_key that must be present for this flag
#                       to be accepted, or None if ungated at every tier
#   used_in           - list of command keys (see COMMANDS below) this flag
#                       is valid on

FLAGS = {
    "date": {
        "names": ["-d", "-date"],
        "values": "dd.mm.yyyy [HH:MM]",
        "default": None,
        "description": "Event date (and optional time). No default - if omitted, the event simply has no date shown at all",
        "gated_by_feature": None,
        "used_in": ["newevent", "editevent"],
    },
    "goingicon": {
        "names": ["-gi", "-goingicon"],
        "values": "<emoji>",
        "default": "👍",
        "description": "Custom Going icon",
        "gated_by_feature": None,
        "used_in": ["newevent", "editevent"],
    },
    "notgoingicon": {
        "names": ["-ni", "-notgoingicon"],
        "values": "<emoji>",
        "default": "❌",
        "description": "Custom Not Going icon",
        "gated_by_feature": None,
        "used_in": ["newevent", "editevent"],
    },
    "limit_event": {
        "names": ["-limit"],
        "values": "N",
        "default": None,
        "description": (
            "caps going+guests across the whole event; once full, new "
            "Going clicks join the Waitlist instead. No default - if "
            "omitted, no cap applies at all"
        ),
        "gated_by_feature": "event_limit",
        "used_in": ["newevent", "editevent"],
    },
    "waitlist": {
        "names": ["-wl", "-waitlist"],
        "values": "visible|hidden|onlycount",
        "default": "hidden",
        "description": (
            "Waitlist visibility in the post (independent of -limit, can "
            "be set/changed on its own). visible: hub's post shows "
            "everyone across every chat; a child chat's post shows only "
            "its own. onlycount: just the total. hidden: nothing shown, "
            "admin-only via /waitlist"
        ),
        "gated_by_feature": "event_limit",
        "used_in": ["newevent", "editevent"],
    },
    "notgoinglist": {
        "names": ["-ngl", "-notgoinglist"],
        "values": "visible|hidden|onlycount",
        "default": "visible",
        "description": "Not Going list visibility in the post",
        "gated_by_feature": None,
        "used_in": ["newevent", "editevent"],
    },
    "maingoinglist": {
        "names": ["-mgl", "-maingoinglist"],
        "values": "visible|hidden|onlycount",
        "default": "onlycount",
        "description": "Going list visibility in this specific share",
        "gated_by_feature": None,
        "used_in": ["shareevent"],
    },
    "sharenotgoinglist": {
        "names": ["-sngl", "-sharenotgoinglist"],
        "values": "visible|hidden|onlycount",
        "default": None,
        "description": (
            "Not Going list visibility override for this share (if "
            "omitted, inherits the event's own -ngl setting)"
        ),
        "gated_by_feature": None,
        "used_in": ["shareevent"],
    },
    "sharewaitlist": {
        "names": ["-swl", "-sharewaitlist"],
        "values": "visible|hidden|onlycount",
        "default": None,
        "description": (
            "Waitlist visibility override for this share (if omitted, "
            "inherits the event's own -wl setting)"
        ),
        "gated_by_feature": None,
        "used_in": ["shareevent"],
    },
    "active": {
        "names": ["-a", "-active"],
        "values": None,
        "default": None,
        "description": "Mark as active. No default - this is an action, not a persistent setting; one of -a/-p is expected on every call",
        "gated_by_feature": None,
        "used_in": ["updateuser"],
    },
    "passive": {
        "names": ["-p", "-passive"],
        "values": None,
        "default": None,
        "description": "Mark as passive. No default - this is an action, not a persistent setting; one of -a/-p is expected on every call",
        "gated_by_feature": None,
        "used_in": ["updateuser"],
    },
    "minlevel": {
        "names": ["-minlevel"],
        "values": "free|pro|admin",
        "default": None,
        "description": "the minimum tier required to use this feature at all. No default - this is a partial-update flag; if omitted, the existing tier requirement is simply left unchanged",
        "gated_by_feature": None,  # owner-only command, gated on OWNER_USER_IDS not a feature
        "used_in": ["updatefeature"],
    },
    "limit_feature": {
        "names": ["-limit"],
        "values": "N",
        "default": None,
        "description": (
            "the cap that applies only while a group is exactly at that "
            "feature's tier (any tier above is always unlimited). 0 "
            "clears it (unlimited)"
        ),
        "gated_by_feature": None,
        "used_in": ["updatefeature"],
    },
    "pro_filter": {
        "names": ["-pro"],
        "values": None,
        "default": None,
        "description": "filters the list to PRO-tier groups only. No default - a presence-only filter; omitted means every group is shown, not a specific fallback value",
        "gated_by_feature": None,
        "used_in": ["allgroups"],
    },
    # clickability is deliberately declared LAST - it's a cross-cutting
    # flag shared by 3 commands, and should always render AFTER each
    # command's own core flags (matching the original hand-written
    # text's convention: command-specific flags first, shared/override
    # flag last), regardless of dict iteration order otherwise.
    "clickability": {
        "names": ["-clc", "-clickability"],
        "values": "on|off",
        "default": "on",
        "description": "whether names in the post are clickable mentions",
        "gated_by_feature": "clickability",
        "used_in": ["newevent", "editevent", "shareevent"],
    },
}


# ---------------------------------------------------------------------------
# Command definitions
# ---------------------------------------------------------------------------
# Purely metadata for now (label + whether it's owner-only) - the actual
# handler function stays in handlers.py/subscription.py/etc. as normal.
# flags_of(key) below is the real integration point most callers want.

COMMANDS = {
    "newevent":      {"label": "/newevent",      "owner_only": False},
    "editevent":     {"label": "/editevent",     "owner_only": False},
    "shareevent":    {"label": "/shareevent",    "owner_only": False},
    "updateuser":    {"label": "/updateuser",    "owner_only": False},
    "updatefeature": {"label": "/updatefeature", "owner_only": True},
    "allgroups":     {"label": "/allgroups",     "owner_only": True},
}


def flags_of(command_key: str) -> list:
    """Every flag entry (as (flag_key, flag_dict) pairs) valid on the
    given command, in FLAGS' own declared order."""
    return [(k, v) for k, v in FLAGS.items() if command_key in v["used_in"]]


def render_flags_detail(command_key: str) -> str:
    """
    Builds the MarkdownV2 "Flags" detail block for one command, in the
    same visual format the 4 hand-written _X_flags_detail_text()
    functions used before this registry existed. One line per flag:
    "-short | -long [values] - description[. Requires a higher tier.]"
    """
    from utils import escape_markdown  # local import avoids a cycle at module load time

    lines = []
    for _key, flag in flags_of(command_key):
        names = " \\| ".join(f"\\-{n.lstrip('-')}" for n in flag["names"])
        values_part = f" {escape_markdown(flag['values'])}" if flag["values"] else ""
        default_part = f"\\(default: {escape_markdown(flag['default'])}\\) " if flag["default"] else ""
        desc = escape_markdown(flag["description"])
        gating = " Requires a higher tier\\." if flag["gated_by_feature"] else ""
        lines.append(f"{names}{values_part} \\- {default_part}{desc}\\.{gating}")
    return "\n".join(lines) + ("\n" if lines else "")
