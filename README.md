# Zencontrol Home Assistant

A comprehensive Home Assistant custom integration for [zencontrol](https://zencontrol.com) application controllers over TPI Advanced.

## Features

* **Easy setup** — find a controller on your subnet automatically at the press of a DALI button
* **Auto-discovery** — lights, groups, buttons, motion sensors, absolute inputs, profiles, and labelled system variables appear automatically after setup
* **Rooms and areas** — group entities into sub-devices by label prefix so rooms map cleanly to Home Assistant areas
* **Live updates** — light levels, colour, scenes, profiles, motion, buttons, and absolute inputs update in Home Assistant as they change on the controller (no polling)
* **Full colour** — all fixtures fully controllable for dimming, temperature and colour where supported, with correct conversion from linear (DALI) to perceptual (HA)
* **Groups** — group control, plus group scene recall via a select entity
* **Scenes** — complete scene recall including colour changes
* **Button events** — short and long press events for controlling automations
* **Motion sensors** — occupancy detections as binary sensors for lighting and presence automations
* **Absolute inputs** — dials, sliders, and other numeric ECD inputs as measurement sensors
* **Profiles** — view and change the active controller profile from Home Assistant
* **System variables** — expose SVs as binary switches or numeric sensors by suffixing SV names with `switch` or `sensor` or `lux sensor`
* **Translations** — UI strings in English, German, French, Danish, Swedish, Polish, Hindi, and Simplified Chinese

## Architecture

This integration is built on top of [`zencontrol-python`](https://github.com/sjwright/zencontrol-python), which is a complete implementation of the TPI Advanced protocol, transport, command API, and entity model. By using this library, the integration has:

* **Reliable networking** — a fully resolved UDP stack with retries and backoff to absorb network challenges
* **Listener-driven state** — a battle-tested event listener wired to locally cached scene settings to keep synchronisation fast and reliable
* **Multicast or unicast** — multicast mode is superior when available; we support fallback to unicast if multicast is blocked
* **Richer discovery** — multicast find-on-LAN, interview of lights/groups/buttons/sensors/absolute inputs/SVs, and many other features are fully implemented
* **Test-driven reliability** — the protocol stack has been exercised against a hardware simulator to ensure that edge cases and time-sensitive bugs are handled correctly

## Requirements

- Home Assistant **2026.3** or later (Python **3.14+**)
- A zencontrol application controller with a **TPI Advanced** license
- Network reachability to the controller (host/port); MAC address is used for identification
- [`zencontrol-python`](https://github.com/sjwright/zencontrol-python)

## How to install

### Install via HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories
2. Repository: `sjwright/zencontrol-homeassistant`, Category: Integration
3. Download **Zencontrol**, then restart Home Assistant
4. Settings → Devices & services → Add integration → **Zencontrol**

Home Assistant installs `zencontrol-python` from PyPI automatically.

### Install manually

Copy `custom_components/zencontrol_tpi` into your Home Assistant `custom_components` directory, restart, then add the integration as above.

The folder name / HA domain (`zencontrol_tpi`) is a legacy identifier and must not be renamed, or existing installs will break.

For local development against an editable library checkout:

```bash
pip install -e /path/to/zencontrol-python
```

## Install for development

```bash
python -m venv .venv
source .venv/bin/activate
pip install homeassistant
pip install -e ../zencontrol-python
./run-ha
```

`./run-ha` starts Home Assistant with `dev-config/` and skips pip-installing `zencontrol-python` so your editable checkout is used. Use `./reset-ha` to wipe the local HA config state.

## License

[MIT](LICENSE)
