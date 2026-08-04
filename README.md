# Zencontrol Home Assistant

A Home Assistant custom integration for [zencontrol](https://zencontrol.com) application controllers over TPI Advanced.

## Features

* **Local control** — LAN only; no zencontrol Cloud dependency for day-to-day use
* **Easy setup** — discover a controller on your subnet at the press of a DALI button
* **Stable entity IDs** — based on controller identity and DALI addressing, so replacing a faulty device won't break automations
* **Runtime discovery** — additional controllers found later can be confirmed and added easily
* **Full device support** — lights, fans, blinds, groups, buttons, motion sensors, profiles, and labelled system variables
* **Rooms and areas** — split a large DALI bus into virtual sub-devices so fixtures can live in different Home Assistant areas
* **Live updates** — levels, colour, scenes, profiles, motion, buttons, and absolute inputs stay in sync with the controller
* **Full colour control** — dimming, colour temperature, and colour where the fixture supports it, with correct conversion between linear DALI levels and perceptual Home Assistant brightness
* **Custom fade times** — transition times in Home Assistant automations are mapped to DALI fade times
* **Scene control** — recall group scenes, plus a per-group select entity for automations on current scene and scene changes
* **All device events** — short and long press button events, occupancy sensors, dials, sliders, and other ECD inputs
* **Profiles** — view and change the active controller profile; profile changes can trigger automations
* **System variables** — expose zen SVs as switches or sensors by suffixing their label with `switch`, `sensor`, or `lux sensor`
* **Fans and blinds** — zencontrol fan and blind controllers are detected by GTIN; for other brands, suffix the location with `fan` or `blind`
* **Controller status** — diagnostic online / starting / unreachable state per controller
* **Translations** — English, German, French, Danish, Swedish, Polish, Hindi, and Simplified Chinese

## Architecture

This integration builds on [`zencontrol-python`](https://github.com/sjwright/zencontrol-python), a complete TPI Advanced stack covering the wire protocol, transport, command API, and entity model. That foundation provides:

* **Reliable networking** — a solid UDP implementation of the TPI Advanced wire protocol, plus a battle-tested event listener
* **Controller workarounds** — strategies for known hardware limits (for example a local scene cache, because the controller is often slow to report scene-derived colour changes)
* **Multicast or unicast** — per controller; multicast when the network allows it, unicast when it does not
* **UDP or TCP commands** — UDP by default; optional per-controller TCP for firmware 2.2.32+
* **Rich discovery** — multicast controller discovery, plus a full interview of lights, groups, buttons, sensors, inputs, and system variables
* **Test-driven reliability** — a large test suite, backed by a [`hardware simulator`](https://github.com/sjwright/zencontrol-simulator), covering edge cases and timing-sensitive behaviour
* **Real-world reliability** — in production for over a year, and used to find and help resolve many bugs in earlier zencontrol firmware

## Requirements

- Home Assistant **2026.3** or later (Python **3.14+**)
- A zencontrol application controller with a **TPI Advanced** license
- Network reachability to the controller

## How to install

### Install via HACS (custom repository)

1. In HACS, open the main ⋮ menu and choose **Custom repositories**.
2. Add `sjwright/zencontrol-homeassistant` with category **Integration**.
3. Find **Zencontrol** in the integration list and choose **Download**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration → Zencontrol**.

### Install manually

1. Copy `custom_components/zencontrol_tpi` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Zencontrol**.

---

## Tips for a good experience

DALI and zencontrol do not always align with Home Assistant assumptions. These tips may reduce friction.

### 1. Prefix devices in zencontrol Cloud

This integration can split one physical controller into virtual sub-devices based on device label prefixes. Splitting lights, switches, and sensors onto a sub-device lets you assign them to a Home Assistant room/area.

Configure sub-devices from the integration page (click **Zencontrol** from the integrations list, or from the device info page). Click the gear icon next to each controller (Home Assistant calls this a "hub") and follow the steps.

* In zencontrol Cloud, device labels are called _locations_. Under **Device Location**, edit locations so devices in the same room share a prefix — for example `Kitchen 1`, `Kitchen Pendant`.
* In addition (or alternatively), if you have a DALI group named `Kitchen`, member lights of that group are treated as if their names start with `Kitchen`. Groups are matched first, so avoid groups that span multiple rooms.
* When adding sub-devices, you can combine several prefixes into one sub-device with a comma-delimited list. Useful when DALI rooms don't map 1:1 with Home Assistant rooms — for example `Kitchen` and `Living` in an open-plan home.
* Be aware: in zencontrol Cloud, **Floor** is a cloud-only concept and is not sent to the controller, so this integration cannot read it.

### 2. Label every instance

Buttons and sensors appear in Home Assistant using their instance labels.

* In zencontrol Cloud, under **Instance types**, label everything under **Push button**, **Absolute input**, **Touchscreen**, **Occupancy sensor**, and **Light sensor**. It takes a little time, but it's worth it. Tip: the grid editor lets you copy one label cell and paste across multiple others, then edit suffixes.
* As with devices, buttons and sensors are assigned to virtual sub-devices by instance-label prefix. Use names like `Kitchen B1` or `Kitchen Pantry`.
* Names only need to be distinct within their own context. It's fine (and works well) to have a light named `Laundry` and a button named `Laundry` with a single instance named `Laundry`.

### 3. Workarounds for zencontrol limits

* Multi-channel ECGs (for example a zencontrol 4-CH PWM dimmer) can only be assigned one label (location) for all channels. As a workaround, this integration accepts comma-separated locations, ordered by logical DALI address — `Garage 1,Garage 2,Garden 1,Garden 2` is split and disambiguated here.
* Light sensor values cannot be read directly over the API. As a workaround, create a matching **System Variable** for each sensor and set the sensor's _Primary target_ to that SV. If you suffix the SV name with `lux sensor`, the integration treats it as a lux sensor.

### 4. Recipe: control individual button LEDs

You may want a wall-button LED driven by Home Assistant — for example to show garage-door or air-conditioner state. While there are API commands for doing this directly, the following is more robust and reliable.

* Create a **System Variable** whose name ends with `switch`. It will appear in Home Assistant as a switch.
* In zencontrol Cloud, under **Instance types → Push button**, set **LED behaviour** to `System Variable N equals 1`, where N is the variable number.

### 5. Recipe: trigger Home Assistant automations from zencontrol sequences

* Create a **System Variable** whose name ends with `switch` (two-way binary) or `sensor` (read-only numeric).
* When zencontrol Cloud changes that SV, Home Assistant updates. Automations can trigger on the switch or sensor.

---

## Development

Clone this repo, create a venv, install Home Assistant in the venv, and run a local instance at `http://localhost:8123`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r dev/requirements.txt
pip install -e ../zencontrol-python
./run-ha
```

Use `./run-ha --reset` to wipe local Home Assistant config state and start fresh.

If [`zencontrol-python`](https://github.com/sjwright/zencontrol-python) is checked out as a sibling directory, it will be used instead of the PyPI release.

You can also check out [`zencontrol-simulator`](https://github.com/sjwright/zencontrol-simulator) as a sibling directory. Refer to its docs for how to run. You can use the simulator on its own, or in addition to real hardware.

### Run the checks

The same three checks run in CI, alongside Home Assistant's `hassfest` and HACS validation:

```bash
ruff check .
pyright custom_components/zencontrol_tpi
pytest -q
```

## License

[MIT](LICENSE)
