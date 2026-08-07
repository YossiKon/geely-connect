"""The YAML this repo ships at people - blueprints, automations, dashboards.

Nothing checked these files before, and two real faults were living in them: a
blueprint that fired `climate.set_temperature` and `climate.set_hvac_mode` back
to back, and - caught while writing the seat-follow-up script -
`target: {entity_id: "{{ repeat.item }}"}`, which Home Assistant's own script
schema rejects outright, so that blueprint would have failed to load for every
user who tried it.

How far the checking goes is limited on purpose. `cv.template` refuses to run
without a live Home Assistant ("Validates schema outside the event loop"), and
`cv.template_complex` then misreports ordinary strings, so a full config
validation of anything containing `{{` produces failures that say nothing about
the file. Configs are therefore validated in full only where they are
template-free; everything else is checked structurally.

The back-to-back check is the one that earns its place. The car executes one
remote command at a time and rejects the next with "the last request has not yet
been executed" - and the rejected command is not queued, it is lost. That fault
has now shipped three times: in `climate.turn_off`, in the pre-heat automation,
and in the precondition blueprint. It is enforced here rather than remembered.
"""
import glob
import os

from run import skip

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Service domains whose calls become a remote command to the car. notify and
# persistent_notification reach the phone, not the vehicle, so two of those in a
# row are harmless and are not listed.
CAR_DOMAINS = {"climate", "lock", "switch", "select", "button", "number", "time"}


def _loader():
    try:
        from homeassistant.util.yaml import loader
    except ImportError:
        skip("homeassistant not installed")
    return loader


def _blueprints():
    return sorted(glob.glob(os.path.join(ROOT, "blueprints", "**", "*.yaml"),
                            recursive=True))


def _automations():
    return sorted(glob.glob(os.path.join(ROOT, "automations", "*.yaml")))


def _call(step):
    return step.get("service") or step.get("action")


def _is_car_command(step):
    call = _call(step)
    return bool(call) and str(call).split(".")[0] in CAR_DOMAINS


def _paused(step):
    return any(k in step for k in ("delay", "wait_template", "wait_for_trigger"))


def _blocks(node, in_repeat=False):
    """Every action sequence in a config, as `(steps, wraps)` pairs.

    A `choose` branch is its own block: its steps never run beside a sibling
    branch's, so two commands in different branches are not adjacent.

    `wraps` marks a block under a `repeat:`, where the last step runs straight
    into the first on the next pass - an adjacency a plain pairwise walk cannot
    see. It propagates inward, so a `choose` inside a loop is a loop body too.
    """
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("sequence", "actions", "action", "then", "else",
                       "default") and isinstance(value, list):
                out.append(([s for s in value if isinstance(s, dict)], in_repeat))
            out.extend(_blocks(value, in_repeat or key == "repeat"))
    elif isinstance(node, list):
        for item in node:
            out.extend(_blocks(item, in_repeat))
    return out


def _adjacent(block, *, wraps=False):
    pairs = list(zip(block, block[1:]))
    # A repeat body runs again immediately, so its last step is followed by its
    # own first step with no gap between them.
    if wraps and len(block) > 1:
        pairs.append((block[-1], block[0]))
    return pairs


def _races(pairs):
    return [(a, b) for a, b in pairs
            if _is_car_command(a) and _is_car_command(b) and not _paused(a)]


# --------------------------------------------------------------- blueprints ---

def test_every_blueprint_loads_the_way_home_assistant_loads_it():
    """BLUEPRINT_SCHEMA is the part that can be checked without a running
    instance: the metadata block, the domain, and every input's selector."""
    loader = _loader()
    from homeassistant.components.blueprint.models import Blueprint
    from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA

    found = _blueprints()
    assert found, "no blueprints found - has the folder moved?"
    for path in found:
        data = loader.load_yaml(path)
        Blueprint(data, expected_domain=data["blueprint"]["domain"], path=path,
                  schema=BLUEPRINT_SCHEMA)


def test_a_template_free_blueprint_validates_after_substitution():
    """What a user actually ends up with. Only inputs without a default are
    filled in, so this is the one-field-and-save case - and it is what catches a
    step that parses as YAML and then fails Home Assistant's schema."""
    loader = _loader()
    from homeassistant.components.automation.config import (
        PLATFORM_SCHEMA as AUTOMATION_SCHEMA)
    from homeassistant.components.blueprint.models import (
        Blueprint, BlueprintInputs)
    from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
    from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA

    checked = []
    for path in _blueprints():
        data = loader.load_yaml(path)
        domain = data["blueprint"]["domain"]
        bp = Blueprint(data, expected_domain=domain, path=path,
                       schema=BLUEPRINT_SCHEMA)
        given = {}
        for name, spec in (data["blueprint"].get("input") or {}).items():
            if spec and "default" in spec:
                continue
            sel = (spec or {}).get("selector") or {}
            ent = sel.get("entity") or {}
            value = f"{ent.get('domain') or 'sensor'}.geely_thing"
            given[name] = [value] if ent.get("multiple") else value
        inputs = BlueprintInputs(bp, {"use_blueprint": {"path": path,
                                                        "input": given}})
        inputs.validate()
        config = inputs.async_substitute()
        if "{{" in str(config):
            continue          # needs a live hass; see the module docstring
        (SCRIPT_ENTITY_SCHEMA if domain == "script" else AUTOMATION_SCHEMA)(config)
        checked.append(os.path.basename(path))
    assert "rapid_climate_with_seats.yaml" in checked, (
        "the seat follow-up script is the blueprint this test exists for; if it "
        f"stopped being template-free, validate it another way. checked={checked}"
    )


# -------------------------------------------------------------- automations ---

def test_the_shipped_automations_are_structurally_sound():
    loader = _loader()
    files = _automations()
    assert files, "no automation files found"
    seen = set()
    for path in files:
        entries = loader.load_yaml(path)
        assert isinstance(entries, list), f"{path} is not a list of automations"
        for entry in entries:
            assert isinstance(entry, dict), f"{path} has a non-mapping entry"
            # An id is what lets Home Assistant keep the automation's state and
            # traces across an edit; without one a rename orphans its history.
            assert entry.get("id"), f"{path}: an automation has no id"
            assert entry["id"] not in seen, f"duplicate automation id {entry['id']}"
            seen.add(entry["id"])
            assert entry.get("alias"), f"{entry['id']} has no alias"
            assert entry.get("triggers") or entry.get("trigger"), (
                f"{entry['id']} has no trigger")
            assert entry.get("actions") or entry.get("action"), (
                f"{entry['id']} has no action")


# ------------------------------------------- the one that keeps coming back ---

def test_no_shipped_yaml_fires_two_car_commands_back_to_back():
    """The car takes one command at a time and the loser of a race is dropped,
    not retried. An owner's climate was left off by exactly this (#19)."""
    loader = _loader()
    offenders = []
    examined = 0
    for path in _blueprints() + _automations():
        data = loader.load_yaml(path)
        for block, wraps in _blocks(data):
            examined += sum(1 for s in block if _is_car_command(s))
            for first, second in _races(_adjacent(block, wraps=wraps)):
                offenders.append(f"{os.path.relpath(path, ROOT)}: "
                                 f"{_call(first)} then {_call(second)}")
    assert not offenders, (
        "two remote commands with no delay between them:\n  " + "\n  ".join(offenders)
    )
    # A sweep that finds nothing to look at passes silently, and a renamed key
    # or a restructured file is exactly how that would happen. The pre-heat and
    # pre-cool automations and the two blueprints below guarantee several.
    assert examined >= 6, (
        f"only {examined} car commands were examined - _blocks() has probably "
        "stopped finding the action sequences, so this test proves nothing"
    )


def test_a_repeat_body_is_recognised_as_one():
    """The sweep passes `wraps` from here, so if this stops marking loop bodies
    the wrap-around check above quietly stops applying. No shipped file has a
    `repeat:` today - this is what keeps the protection real when one does."""
    config = {"sequence": [{"repeat": {"for_each": ["a"], "sequence": [
        {"action": "lock.lock"}, {"choose": [{"conditions": [], "sequence": [
            {"action": "switch.turn_on"}]}]}]}}]}
    blocks = dict((tuple(_call(s) for s in steps), wraps)
                  for steps, wraps in _blocks(config))
    assert blocks[(None,)] is False, "the outer sequence is not a loop body"
    assert blocks[("lock.lock", None)] is True, "the repeat body was not marked"
    assert blocks[("switch.turn_on",)] is True, (
        "a choose nested inside a loop is still inside the loop")


def test_the_race_checker_sees_a_loop_wrapping_onto_itself():
    """Guards the checker rather than the YAML. A repeat body that ends and
    begins with a command races itself on the second pass, and a plain pairwise
    walk never compares the last step with the first."""
    safe = [{"service": "select.select_option"}, {"delay": {"seconds": 5}}]
    assert not _races(_adjacent(safe, wraps=True))
    racing = [{"service": "select.select_option"},
              {"service": "climate.set_temperature"}]
    assert _races(_adjacent(racing))
    # A body that is safe read top to bottom and still races itself: the last
    # command runs straight into the first one on the next pass. A pairwise walk
    # finds nothing here.
    loop = [{"action": "lock.lock"}, {"delay": {"seconds": 5}},
            {"action": "switch.turn_on"}]
    assert not _races(_adjacent(loop))
    assert len(_races(_adjacent(loop, wraps=True))) == 1
