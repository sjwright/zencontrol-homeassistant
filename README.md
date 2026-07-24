# Zencontrol Home Assistant

A comprehensive Home Assistant custom integration for [zencontrol](https://zencontrol.com) application controllers over TPI Advanced.

## Features

* **Easy setup** — find a controller on your subnet automatically at the press of a DALI button
* **Auto-discovery** — lights, groups, buttons, motion sensors, absolute inputs, profiles, and labelled system variables appear automatically after setup
* **Rooms and areas** — group entities into virtual sub-devices so you can map rooms to Home Assistant areas
* **Live updates** — light levels, colour, scenes, profiles, motion, buttons, and absolute inputs update in Home Assistant as they change on the controller
* **Full colour** — all fixtures fully controllable for dimming, temperature, and colour (where supported) with correct conversion between linear (DALI) and perceptual (HA) levels
* **Groups & scenes** — full group control, plus group scene recall via native scene entities
* **Button events** — trigger automations with short and long press events
* **Motion sensors** — occupancy detections as binary sensors for presence-based automations
* **Absolute inputs** — dials, sliders, and other numeric ECD inputs as measurement sensors
* **Profiles** — view and change the active controller profile
* **System variables** — expose SVs as binary switches or numeric sensors by suffixing SV names with `switch`, `sensor`, or `lux sensor`
* **Translations** — English, German, French, Danish, Swedish, Polish, Hindi, and Simplified Chinese

## Architecture

This integration is built on top of [`zencontrol-python`](https://github.com/sjwright/zencontrol-python), a complete and mature implementation of the TPI Advanced protocol, transport, command API, and entity model. By using this library, the integration has:

* **Reliable networking** — a fully resolved UDP stack implementation of the TPI Advanced wire protocol, plus a battle-tested event listener
* **Defies controller limitations** - various strategies are employed to work around known limitations of the controller hardware (e.g. a local scene setting cache to maintain sync, as the controller is often slow to notify of scene-derived colour changes)
* **Multicast or unicast** — multicast mode is superior when available; fallback to unicast is supported in network environments where multicast is unsupported
* **Richer discovery** — controller discovery through multicast; extended interview of lights/groups/buttons/sensors/inputs/SVs, and many other features fully implemented
* **Test-driven reliability** — an extensive test suite is combined with a [`hardware simulator`](https://github.com/sjwright/zencontrol-simulator) to ensure that edge cases and time-sensitive bugs are handled correctly
* **Extensive real-world testing** — actively used in production for over a year, and has been instrumental in closing numerous bugs in the zencontroller firmware

## Requirements

- Home Assistant **2026.3** or later (Python **3.14+**)
- A zencontrol application controller with a **TPI Advanced** license
- Network reachability to the controller (host/port)

## How to install

### Install via HACS (custom repository)

1. In HACS, open the main ⋮ menu, select Custom repositories
2. Add the custom repository `sjwright/zencontrol-homeassistant` with the category of `Integration`.
3. Find `Zencontrol` in the list of integrations. From its side menu, select Download.
4. Restart Home Assistant.
5. Settings → Devices & services → Add integration → **Zencontrol**

Home Assistant installs `zencontrol-python` from PyPI automatically.

### Install manually

1. Copy `custom_components/zencontrol_tpi` into your Home Assistant `custom_components` directory
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **Zencontrol**

---

## Tips for a good experience

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

---

### Set up development environment

Check out this repo in a suitable directory, create and activate a venv, install homeassistant within the venv, and it will run an empty instance of Home Assistant locally, accessible from `http://localhost:8123`.

If you check out [`zencontrol-python`](https://github.com/sjwright/zencontrol-python) in a sibling directory, it will use that instead of downloading the release version via pip.

You can also check out [`zencontrol-simulator`](https://github.com/sjwright/zencontrol-simulator) in a sibling directory. See its documentation for how to run. You can use the simulator to test the Home Assistant integration support without physical hardware, or combined with physical hardware to test multiple controller support.

```bash
python -m venv .venv
source .venv/bin/activate
pip install homeassistant
pip install -e ../zencontrol-python
./run-ha
```

Use `./reset-ha` to wipe the local HA config state.

## License

[MIT](LICENSE)
