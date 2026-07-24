# Zencontrol Home Assistant

A comprehensive Home Assistant custom integration for [zencontrol](https://zencontrol.com) application controllers over TPI Advanced.

## Features

* **Easy setup** — find a controller on your subnet automatically at the press of a DALI button
* **Auto-discovery** — lights, groups, buttons, motion sensors, absolute inputs, profiles, and labelled system variables appear automatically after setup
* **Rooms and areas** — group entities into sub-devices by label prefix so rooms map cleanly to Home Assistant areas
* **Live updates** — light levels, colour, scenes, profiles, motion, buttons, and absolute inputs update in Home Assistant as they change on the controller (no polling)
* **Full colour** — all fixtures fully controllable for dimming, temperature, and colour where supported, with correct conversion from linear (DALI) to perceptual (HA)
* **Groups** — group control, plus group scene recall via native scene entities
* **Scenes** — DALI scene recall with fast UI performance via scene caching at the library level
* **Button events** — short and long press events for controlling automations
* **Motion sensors** — occupancy detections as binary sensors for lighting and presence automations
* **Absolute inputs** — dials, sliders, and other numeric ECD inputs as measurement sensors
* **Profiles** — view and change the active controller profile from Home Assistant
* **System variables** — expose SVs as binary switches or numeric sensors by suffixing SV names with `switch`, `sensor`, or `lux sensor`
* **Translations** — UI strings in English, German, French, Danish, Swedish, Polish, Hindi, and Simplified Chinese

## Architecture

This integration is built on top of [`zencontrol-python`](https://github.com/sjwright/zencontrol-python), a complete implementation of the TPI Advanced protocol, transport, command API, and entity model. By using this library, the integration has:

* **Reliable networking** — a fully resolved UDP stack with retries and backoff to absorb network challenges
* **Listener-driven state** — a battle-tested event listener wired to locally cached scene settings to keep synchronisation fast and reliable
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

---

## Tips for a good time

There are numerous ways in which DALI generally — and zencontrol specifically — nominally misalign with Home Assistant assumptions. In order to minimise grief, the following advice is offered:

### 1. Prefix devices in zencontrol cloud

This integration supports splitting a physical controller into any number of virtual sub-devices based on string prefixes. Separating the lights/switches/sensors in a room into a virtual sub-device allows you to assign them an area in Home Assistant.

You can configure sub-devices from the integration screen (click on "zencontrol" from the integrations list or from the device info). Click the gear icon next to each controller (which HA describes as a "hub") and you will be stepped through the process.

* In ZC cloud, device labels are referred to as _locations._ Under `Device Location`, ensure all lights, switches, and sensors in that room begin with the room name, e.g. `Kitchen 1`, `Kitchen Pendant`.
* In addition (or alternatively), if you have a DALI group named `Kitchen`, all lights in that group will be treated as though their name is prefixed with `Kitchen`.
* Sometimes your ZC rooms won't perfectly align with HA areas. When adding sub-devices within the integration, you can combine multiple prefixes into one sub-device (e.g. `Kitchen` and `Living` in an open-plan home). 
* Be aware that within ZC cloud, `Floor` is a cloud-only property and not sent to the controller. You cannot use this value for arranging or disambiguating in HA.

### 2. Assign labels to all instances too

All buttons and sensors will be labelled in Home Assistant based on their instance label.

* In ZC cloud, under `Instance types`, within `Push button`, `Absolute input`, `Touchscreen`, `Occupancy sensor`, and `Light sensor`, assign labels to everything. It can be a small effort, but it's worth it. Handy tip: you can copy one label cell and paste onto multiple label cells. Edit to add suffixes after.
* As with devices, buttons and sensors will be assigned into virtual sub-devices based on the prefix of their instance label. Label all buttons in a kitchen with room prefixes `Kitchen B1`, `Kitchen Pantry` etc. These don't need to be distinct from light names (locations), so if you have one light named `Laundry` and one push button named `Laundry`, things will work perfectly.

### 3. Workarounds for zencontrol limitations

* Multi-channel ECGs (e.g. zencontrol 4-CH PWM dimmer) cannot have unique names assigned to each channel in ZC cloud. This integration works around this limitation by supporting comma-delimited names, ordered by DALI address number. Assigning it the location `Garage 1,Garage 2,Garden 1,Garden 2` would result in the four channels receiving unique labels.
* Light sensor values cannot be read directly via the API. You can work around this by creating a matching `System Variable` for each light sensor and assigning the sensor's _Primary target_ to that SV. If you suffix the SV name with `lux sensor`, this integration will treat the SV as a lux sensor.

### 4. Recipe: control the illumination of individual buttons

Sometimes you might want a wall button LED to be controlled by Home Assistant, for example to notify on the state of a garage door, or whether an air conditioner is running.

* Create a `System Variable` and suffix its name with `switch`. This will show up in HA as a simple switch.
* In ZC cloud, under `Instance types` > `Push button`, set the `LED behaviour` to `System Variable N equals 1`, where N is the variable number.

### 5. Recipe: trigger HA automations from ZC sequences

* Create a `System Variable` and suffix its name with `switch` (for a two-way binary switch) or `sensor` (for read-only numeric state).
* In ZC cloud, changing the value of this SV will reflect in HA. You can fire HA automations based on changes to that switch or sensor.

## License

[MIT](LICENSE)
