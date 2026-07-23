"""Tests for label-prefix sub-devices."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.zencontrol_tpi.sub_devices import (
    SubDeviceDef,
    build_assignments,
    match_sub_device,
    parse_sub_device_prefixes,
    prefix_matches,
    sub_device_from_prefixes,
    sub_devices_from_config,
    sub_devices_to_config,
    validate_sub_device_prefixes,
)


def test_parse_sub_device_prefixes_trims_whitespace() -> None:
    assert parse_sub_device_prefixes(" Foo, Bar ") == ["Foo", "Bar"]
    assert parse_sub_device_prefixes("Kitchen, Living ,Lounge") == [
        "Kitchen",
        "Living",
        "Lounge",
    ]
    assert parse_sub_device_prefixes("  , , ") == []


def test_prefix_matches_word_boundary() -> None:
    assert prefix_matches("Emily", "Emily")
    assert prefix_matches("Emily 1", "Emily")
    assert prefix_matches("emily: left", "Emily")
    assert not prefix_matches("Emilys light", "Emily")
    assert not prefix_matches("Emily1A", "Emily")
    assert not prefix_matches("Kitchenette", "Kitchen")


def test_match_sub_device_longest_prefix() -> None:
    devices = [
        SubDeviceDef("kit", "Kit", ("Kit",)),
        SubDeviceDef("kitchen", "Kitchen", ("Kitchen", "Lounge")),
    ]
    assert match_sub_device("Kitchen island", devices).id == "kitchen"
    assert match_sub_device("Kit", devices).id == "kit"
    assert match_sub_device("Garage", devices) is None


def test_validate_rejects_duplicate_prefix() -> None:
    existing = [SubDeviceDef("kitchen", "Kitchen", ("Kitchen", "Lounge"))]
    assert validate_sub_device_prefixes(existing, ["Lounge"]) == "duplicate_prefix"
    assert validate_sub_device_prefixes(existing, []) == "empty_prefixes"
    assert validate_sub_device_prefixes(existing, ["Emily"]) is None


def test_sub_device_from_prefixes_uses_first_name() -> None:
    device = sub_device_from_prefixes(["Kitchen", "Living", "Lounge"])
    assert device is not None
    assert device.name == "Kitchen"
    assert device.prefixes == ("Kitchen", "Living", "Lounge")


def test_sub_device_area_id_roundtrip() -> None:
    device = SubDeviceDef(
        "kitchen", "Kitchen", ("Kitchen",), area_id="area_kitchen"
    )
    raw = sub_devices_to_config([device])
    assert raw[0]["area_id"] == "area_kitchen"
    loaded = sub_devices_from_config(raw)
    assert loaded[0].area_id == "area_kitchen"

    no_area = SubDeviceDef("garage", "Garage", ("Garage",))
    raw2 = sub_devices_to_config([no_area])
    assert "area_id" not in raw2[0]
    assert sub_devices_from_config(raw2)[0].area_id is None


def test_group_membership_overrides_light_name() -> None:
    ctrl = SimpleNamespace(name="house")
    emily = SubDeviceDef("emily", "Emily", ("Emily",))
    kitchen = SubDeviceDef("kitchen", "Kitchen", ("Kitchen",))

    light = SimpleNamespace(
        address=SimpleNamespace(controller=ctrl, number=3),
        sub_label="Kitchen spot",
        label="Kitchen spot",
    )
    group = SimpleNamespace(
        address=SimpleNamespace(controller=ctrl, number=1),
        label="Emily upstairs",
        lights=[light],
    )

    assignments = build_assignments(
        controller_sub_devices={"house": [emily, kitchen]},
        lights=[light],
        groups=[group],
        buttons=[],
        motion_sensors=[],
        sysvars=[],
    )
    assert assignments["group:house:1"] == "emily"
    assert assignments["light:house:3"] == "emily"


def test_controller_identifier_prefers_normalized_mac() -> None:
    from custom_components.zencontrol_tpi.const import DOMAIN
    from custom_components.zencontrol_tpi.entity import controller_identifier

    ctrl = SimpleNamespace(name="house", mac="aa-bb-cc-dd-ee-ff")
    assert controller_identifier(ctrl) == (DOMAIN, "AA:BB:CC:DD:EE:FF")

    no_mac = SimpleNamespace(name="house", mac=None)
    assert controller_identifier(no_mac) == (DOMAIN, "house")


def test_ungrouped_light_uses_name() -> None:
    ctrl = SimpleNamespace(name="house")
    kitchen = SubDeviceDef("kitchen", "Kitchen", ("Kitchen",))
    light = SimpleNamespace(
        address=SimpleNamespace(controller=ctrl, number=5),
        sub_label=None,
        label="Kitchen pendant",
    )
    assignments = build_assignments(
        controller_sub_devices={"house": [kitchen]},
        lights=[light],
        groups=[],
        buttons=[],
        motion_sensors=[],
        sysvars=[],
    )
    assert assignments["light:house:5"] == "kitchen"
