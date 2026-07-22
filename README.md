# Zencontrol TPI

Home Assistant custom integration for [zencontrol](https://zencontrol.com) application controllers over TPI Advanced.

## Currently supports

- **Multiple controllers** in one integration (add during setup or later via Configure)
- **Sub-devices** by label prefix (Configure → controller → sub-device) for assigning rooms/areas
- **Profiles**
- **Lights**
- **Groups**, including scene recall for groups
- **Button events**
- **System variables**, where the SV name is suffixed with `switch` or `sensor`

## Requirements

- A zencontrol application controller with a **TPI Advanced** license
- Network reachability to the controller (host/port); MAC address is used for identification
- [`zencontrol-python`](https://github.com/sjwright/zencontrol-python)

## Install

### HACS (custom repository)

1. HACS → Integrations → ⋮ → Custom repositories
2. Repository: `sjwright/zencontrol-homeassistant`, Category: Integration
3. Download **Zencontrol TPI**, then restart Home Assistant
4. Settings → Devices & services → Add integration → **Zencontrol**

Home Assistant installs `zencontrol-python` from PyPI automatically.

### Manual

Copy `custom_components/zencontrol_tpi` into your Home Assistant `custom_components` directory, restart, then add the integration as above.

The folder name / HA domain (`zencontrol_tpi`) is a legacy identifier and must not be renamed, or existing installs will break.

For local development against an editable library checkout:

```bash
pip install -e /path/to/zencontrol-python
```

## Development

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
