"""Build the voice tool catalog from registered devices and their API definitions."""

import asyncio
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

CHANNEL = re.compile(r"switch_[1-9][0-9]*")
SCALAR_TYPES = {"boolean", "integer", "number", "string"}
AUTO_CONTROL_CATEGORIES = {"dj", "dd", "kg", "cz"}
AUTO_REFRESH_INTERVAL = 300
_last_refresh = float("-inf")
_last_topology_sync = float("-inf")


@dataclass(frozen=True)
class Answer:
    title: str
    detail: str
    status: str = "info"
    options: tuple[str, ...] = ()


def tool(name, description, properties):
    return {
        "type": "function", "name": name, "description": description, "strict": True,
        "parameters": {
            "type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False,
        },
    }


TARGETS = {"device_ids": {"type": "array", "items": {"type": "string"}}}
CAPABILITY = {"capability": {"type": ["string", "null"]}}
HSV = {
    "type": "object",
    "properties": {key: {"type": "integer"} for key in ("h", "s", "v")},
    "required": ["h", "s", "v"], "additionalProperties": False,
}
TOOLS = [
    tool("ask_clarification", "Ask one question before acting. Include every matching catalog option.", {
        "question": {"type": "string"},
        "scope": {"type": "string", "enum": ["device", "channel", "room", "other"]},
        "candidates": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string"},
                "channel": {"type": ["string", "null"]},
            },
            "required": ["device_id", "channel"],
            "additionalProperties": False,
        }},
    }),
    tool("explain_no_action", "Report why no device operation can be safely selected.", {
        "reason": {"type": "string", "enum": [
            "device_not_found", "unsupported_control",
        ]},
    }),
    tool("room_lights", "Read or set the lights in one exact Smart Life room; null reads power.", {
        "room": {"type": "string"},
        "home": {"type": ["string", "null"]},
        "on": {"type": ["boolean", "null"]},
    }),
    tool("get_device_status", "Read current device or channel state; use null for all readings.", {
        **TARGETS, **CAPABILITY,
    }),
    tool("get_power_history", "Check current power and the latest accepted power command for a device.", TARGETS),
    tool("get_aliases", "List device aliases and named channels for the specified devices.", TARGETS),
    tool("get_device_controls", "List the available writable controls for the specified devices.", TARGETS),
    tool("set_light_color", "Set mapped HSV light color and optionally power on or off in one command.", {
        **TARGETS, "capability": {"type": "string"}, "color": HSV,
        "on": {"type": ["boolean", "null"]},
    }),
    tool("set_device_control", "Set one advertised writable capability on the selected devices.", {
        **TARGETS,
        "capability": {"type": "string"},
        "value": {"anyOf": [
            {"type": "boolean"}, {"type": "number"}, {"type": "string"},
            HSV,
        ]},
        "format": {"type": "string", "enum": ["value", "percent"]},
    }),
]


def result_json(response):
    response.raise_for_status()
    return response.json()


def hsv_limits(code, definition, limits):
    if code not in ("colour_data", "colour_data_v2") or definition["type"] != "Json":
        return None
    if set(limits) != {"h", "s", "v"}:
        return None
    if not all(
        isinstance(field, dict) and field.get("min") == 0 and field.get("step") == 1
        and field.get("scale", 0) == 0
        and field.get("max") in ((360,) if key == "h" else (255, 1000))
        for key, field in limits.items()
    ):
        return None
    return {key: field["max"] for key, field in limits.items()}


def cloud_controls(cloud):
    definitions = cloud["controls"]["definitions"]
    result = {}
    for kind in ("status_range", "functions"):
        for definition in definitions[kind]:
            code = definition["code"]
            limits = definition.get("values") or {}
            if isinstance(limits, str):
                try:
                    limits = json.loads(limits)
                except ValueError:
                    limits = {}
            if not isinstance(limits, dict):
                limits = {}
            color = hsv_limits(code, definition, limits)
            if color:
                result[code] = {
                    "type": "object", "encoding": "cloud_hsv", "cloud_hsv": color,
                    "readable": True, "writable": kind == "functions",
                }
                continue
            value_type = {
                "Boolean": "boolean", "Integer": "integer", "Enum": "string", "String": "string",
            }.get(definition["type"])
            if value_type:
                result[code] = {
                    "type": value_type, "readable": True,
                    "writable": kind == "functions" and definition["type"] != "String",
                    "minimum": limits.get("min"), "maximum": limits.get("max"),
                    "step": limits.get("step"), "enum": limits.get("range"),
                    "unit": limits.get("unit"), "cloud_scale": limits.get("scale", 0),
                }
    mode = result.get("work_mode", {})
    if not mode.get("writable") or "colour" not in (mode.get("enum") or []):
        for spec in result.values():
            if "cloud_hsv" in spec:
                spec["writable"] = False
    return result


def catalog_entry(name, row, remote, path, use_cloud):
    definitions = remote["controls"]["definitions"] if remote else {}
    channels = {
        spec["code"]: spec["user_label"]
        for spec in definitions.get("functions", []) + definitions.get("status_range", [])
        if CHANNEL.fullmatch(spec["code"]) and spec.get("user_label")
    }
    return {
        "name": name,
        "aliases": sorted(set(row["aliases"]) | {row["name"]}
                          | set(remote.get("aliases", []) if remote else [])),
        "channels": channels,
        "capabilities": cloud_controls(remote) if use_cloud and remote else row.get("capabilities", {}),
        "path": path,
        "cloud": use_cloud, "cloud_metadata": remote is not None,
    }


async def refresh_cloud_definitions(backend, remote):
    if remote["controls"].get("ready"):
        return
    try:
        remote["controls"]["definitions"] = result_json(await backend.get(
            f"cloud/devices/{quote(remote['id'], safe='')}/functions",
            params={"provider": "sharing"},
        ))
        remote["controls"]["ready"] = True
    except (httpx.HTTPError, ValueError):
        pass


async def fetch_cloud(backend, auto_tuya):
    global _last_refresh
    params = {"provider": "sharing"}
    refresh = auto_tuya and time.monotonic() - _last_refresh >= AUTO_REFRESH_INTERVAL
    if refresh:
        _last_refresh = time.monotonic()
        try:
            return result_json(await backend.get("cloud/devices", params={**params, "refresh": "true"}))
        except (httpx.HTTPError, ValueError):
            pass
    return result_json(await backend.get("cloud/devices", params=params, timeout=5))


async def room_placements(backend, *, allow_sync=True):
    global _last_topology_sync
    try:
        topology = result_json(await backend.get("cloud/topology"))
        timestamp = topology.get("synced_at")
        stale = not timestamp or (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).days >= 1
        if (allow_sync and stale and topology["job"]["status"] != "running"
                and time.monotonic() - _last_topology_sync >= AUTO_REFRESH_INTERVAL):
            _last_topology_sync = time.monotonic()
            await backend.post("cloud/topology/sync", timeout=4)
        rooms = result_json(await backend.get("rooms", params={"limit": 500}))
    except (httpx.HTTPError, ValueError):
        return {}
    room_names = {room["id"]: room["name"] for room in rooms}
    homes = {home["home_id"]: home["name"] for home in topology["homes"]}
    return {
        row["device_id"]: (
            (row["home_id"], row["cloud_room_id"]), homes[row["home_id"]], room_names[row["room_id"]],
        )
        for row in topology["placements"]
        if row["home_id"] in homes and row["room_id"] in room_names
    }


def apply_room(device, remote, placement, room_names):
    if not placement:
        return
    device["room_key"], device["home"], device["room"] = placement
    name = remote["name"].casefold().strip()
    room = device["room"].casefold()
    # Some fixtures retain a different room name in Smart Life. Require an
    # explicit device name for these and for extractors that report as bulbs.
    device["room_light"] = (
        remote["category"] == "dj" and not any(word in name for word in ("extract", "hood", "fan"))
        and not any(other != room and name.startswith(other + " ") for other in room_names)
    )


async def load_catalog(backend, labels, cloud_ids, cloud_labels=None, *, auto_tuya=False, delegated=False):
    cloud_labels = cloud_labels or {}
    rows = result_json(await backend.get("devices")) if labels or delegated else []
    if delegated:
        labels = {str(row["id"]): labels.get(str(row["id"]), row["name"]) for row in rows}
    try:
        cloud = {row["id"]: row for row in await fetch_cloud(backend, auto_tuya)}
    except (httpx.HTTPError, ValueError):
        cloud = {}

    configured = {row["device_id"]: row for row in rows if str(row["id"]) in labels}
    cloud_targets = set(cloud_labels) | {
        device_id for device_id, row in configured.items() if str(row["id"]) in cloud_ids
    }
    await asyncio.gather(*(refresh_cloud_definitions(backend, cloud[ident])
                           for ident in cloud_targets if ident in cloud))
    placements = await room_placements(backend, allow_sync=not delegated) if auto_tuya and cloud else {}
    room_names = {placement[2].casefold() for placement in placements.values()}
    catalog = {}
    for row in rows:
        ident = str(row["id"])
        if ident not in labels or not row["enabled"]:
            continue
        remote = cloud.get(row["device_id"])
        use_cloud = ident in cloud_ids
        if use_cloud and not remote:
            row = {**row, "capabilities": {}}
        path = f"cloud/devices/{quote(row['device_id'], safe='')}" if use_cloud else f"devices/{ident}"
        catalog[ident] = catalog_entry(labels[ident], row, remote, path, use_cloud)
        if remote:
            apply_room(catalog[ident], remote, placements.get(row["device_id"]), room_names)
    discover = {ident: remote["name"] for ident, remote in cloud.items()} if auto_tuya else {}
    for index, (device_id, name) in enumerate({**discover, **cloud_labels}.items(), start=1):
        remote = cloud.get(device_id)
        if remote and device_id not in configured:
            path = f"cloud/devices/{quote(device_id, safe='')}"
            device = catalog_entry(name, remote, remote, path, True)
            device["remote"] = remote
            device["category"] = remote["category"]
            device["read_only"] = device_id not in cloud_labels and remote["category"] not in AUTO_CONTROL_CATEGORIES
            apply_room(device, remote, placements.get(device_id), room_names)
            catalog[f"cloud-{index}"] = device
    return catalog


def can_write(spec):
    return spec["writable"] and (
        spec["type"] in SCALAR_TYPES or spec.get("encoding", "raw") != "raw"
    )


def model_catalog(catalog):
    fields = ("type", "readable", "minimum", "maximum", "step", "enum", "unit", "encoding", "cloud_scale")
    return {
        ident: {
            "name": device["name"], "aliases": device["aliases"], "channels": device["channels"],
            **{key: device[key] for key in ("room", "home", "room_light", "category", "read_only") if key in device},
            "controls": {
                code: {
                    **{field: spec[field] for field in fields if spec.get(field) is not None},
                    "writable": not device.get("read_only") and can_write(spec),
                }
                for code, spec in device["capabilities"].items()
            },
        }
        for ident, device in catalog.items()
    }


def label_for(device, code):
    labels = {
        "switch": "Power", "switch_led": "Power", "bright_value_v2": "Brightness",
        "temp_value_v2": "Color temperature", "colour_data_v2": "Color",
        "colour_data": "Color", "work_mode": "Mode",
    }
    return device["channels"].get(code) or labels.get(code, code.replace("_", " "))


def display_value(value, spec):
    if type(value) is bool:
        return "on" if value else "off"
    if type(value) in (int, float):
        scale = spec.get("cloud_scale", 0)
        if scale:
            value = round(value / 10 ** scale, scale)
        return f"{value}{' ' + spec['unit'] if spec.get('unit') else ''}"
    if isinstance(value, str) and len(value) <= 60:
        return value
    if isinstance(value, dict) and set(value) == {"h", "s", "v"}:
        return f"hue {value['h']}, saturation {value['s']}, color level {value['v']}"
    return "structured value"


def checked_value(spec, value, value_format):
    kind = spec["type"]
    lower, upper, step = (spec.get(field) for field in ("minimum", "maximum", "step"))
    if value_format == "percent":
        if (
            kind not in {"integer", "number"} or type(value) not in (int, float)
            or not 0 <= value <= 100 or lower is None or upper is None
        ):
            raise ValueError("requires a mapped numeric range and a percentage from 0 to 100")
        if spec.get("unit") == "%" and not spec.get("cloud_scale"):
            value = max(lower, min(value, upper))
        else:
            value = lower + (upper - lower) * value / 100
        if step:
            value = round(lower + round((value - lower) / step) * step, 9)
        elif kind == "integer":
            value = round(value)
    elif value_format != "value":
        raise ValueError("unsupported value format")

    if kind in {"integer", "number"}:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("requires a number")
        if (lower is not None and value < lower) or (upper is not None and value > upper):
            raise ValueError("value is outside its mapped range")
        if step:
            ticks = (value - (lower or 0)) / step
            if not math.isclose(ticks, round(ticks), rel_tol=0, abs_tol=1e-6):
                raise ValueError("value does not match its mapped step")
        if kind == "integer":
            if value != int(value):
                raise ValueError("requires a whole number")
            value = int(value)
    elif kind == "boolean" and type(value) is not bool:
        raise ValueError("requires on or off")
    elif kind == "string" and (not isinstance(value, str) or len(value) > 128):
        raise ValueError("requires a short text or enum value")
    elif kind == "object" and (
        not isinstance(value, dict) or set(value) != {"h", "s", "v"}
        or any(type(value[key]) is not int or not 0 <= value[key] <= limit
               for key, limit in (("h", 360), ("s", 1000), ("v", 1000)))
    ):
        raise ValueError("requires a supported HSV color")
    if spec.get("enum") is not None and value not in spec["enum"]:
        raise ValueError("value is outside its mapped choices")
    return value


async def read_status(backend, device, code):
    name, path, specs = device["name"], device["path"], device["capabilities"]
    if code is not None and (code not in specs or not specs[code]["readable"]):
        return Answer(name, f"No readable {code} mapping.", "unavailable")
    if not specs:
        return Answer(name, "No status mapping for this connection.", "unavailable")
    try:
        if device["cloud"]:
            response = result_json(await backend.get(
                f"{path}/status", params={"refresh": "true", "provider": "sharing"},
            ))
        else:
            response = result_json(await backend.get(f"{path}/state", params={"refresh": "true"}))
        if device["cloud"]:
            if not response["online"]:
                return Answer(name, "Tuya Sharing reports the device offline.", "offline")
            readings = response["status"]
        else:
            readings = {
                key: reading["value"] for key, reading in response["values"].items()
                if reading["known"] and not reading["stale"]
            }
    except (httpx.HTTPError, ValueError, KeyError):
        return Answer(name, "Could not refresh the device state.", "unavailable")
    values = [
        f"{label_for(device, key)}: {display_value(value, specs[key])}"
        for key, value in readings.items()
        if key in specs and specs[key]["readable"] and (code is None or key == code)
    ]
    if values:
        return Answer(name, f"{', '.join(values)}.", "state")
    return Answer(name, "No current reading is available.", "unavailable")


async def hydrate_device(backend, device):
    remote = device.get("remote")
    if remote and not remote["controls"].get("ready"):
        await refresh_cloud_definitions(backend, remote)
        device.update(catalog_entry(device["name"], remote, remote, device["path"], True))


def power_code(device):
    specs = device["capabilities"]
    power = [code for code, spec in specs.items()
             if spec["type"] == "boolean" and (code in ("switch_led", "switch") or CHANNEL.fullmatch(code))]
    return next((code for code in ("switch_led", "switch") if code in power),
                power[0] if len(power) == 1 else "power")


def clarification(catalog, question, scope, candidates):
    options = []
    for candidate in candidates:
        device = catalog.get(candidate["device_id"])
        if device is None:
            continue
        room, home = device.get("room"), device.get("home")
        location = " · ".join(part for part in (room, home) if part)
        if scope == "room":
            if room:
                options.append(location)
            continue
        if scope == "other":
            continue
        name = device["name"]
        if scope == "channel":
            channel = candidate["channel"]
            if not channel or not CHANNEL.fullmatch(channel):
                continue
            if channel not in device["channels"] and channel not in device["capabilities"]:
                continue
            label = device["channels"].get(channel) or f"Channel {channel.split('_')[1]}"
            name = f"{name} · {label}"
        options.append(f"{name} ({location})" if location else name)
    return Answer("Home Voice", question, "clarify", tuple(dict.fromkeys(options)))


async def room_lights(backend, catalog, args, selected):
    room, home, on = args["room"], args["home"], args["on"]
    if not isinstance(room, str) or (home is not None and not isinstance(home, str)):
        raise ValueError("Invalid room")
    if on is not None and type(on) is not bool:
        raise ValueError("Invalid power value")
    candidates = [
        (ident, device) for ident, device in catalog.items()
        if device.get("room", "").casefold() == room.strip().casefold()
        and (home is None or device["home"].casefold() == home.strip().casefold())
    ]
    if len({device["room_key"] for _, device in candidates}) > 1:
        return [clarification(catalog, "Which room did you mean?", "room", [
            {"device_id": ident, "channel": None} for ident, _ in candidates
        ])]
    devices = [device for _, device in candidates if device["room_light"]]
    if not devices:
        return [Answer(room, "No lights found in this Smart Life room.", "unavailable")]
    selected.extend((device["path"], None) for device in devices)
    await asyncio.gather(*(hydrate_device(backend, device) for device in devices))
    if on is None:
        return await asyncio.gather(*(read_status(backend, device, power_code(device)) for device in devices))
    return await set_controls(backend, devices, "power", on, "value")


async def read_power_history(backend, device):
    code = power_code(device)
    state = await read_status(backend, device, code)
    spec = device["capabilities"].get(code, {})
    key = code if device["cloud"] else spec.get("dp")
    ident = device["path"].rsplit("/", 1)[-1]
    try:
        history = result_json(await backend.get(
            f"activity/devices/{ident}",
            params={"provider": "sharing" if device["cloud"] else "local", "limit": 100},
        ))
    except (httpx.HTTPError, ValueError):
        return Answer(state.title, f"{state.detail} Command history is unavailable.", state.status)
    for event in history["events"]:
        values = event["value"]
        if event["kind"] == "command" and event["outcome"] == "accepted" and isinstance(values, dict):
            power = values.get(key)
            if type(power) is bool:
                timestamp = datetime.fromisoformat(event["at"]).astimezone(timezone.utc)
                return Answer(
                    state.title, f"{state.detail} The latest recorded power command was "
                    f"{'on' if power else 'off'} at {timestamp:%H:%M UTC on %d %b}.", state.status,
                )
    return Answer(state.title, f"{state.detail} No recent accepted power command in the history.", state.status)


async def execute(backend, catalog, call, *, selected=None):
    name = call["name"]
    if name not in {tool["name"] for tool in TOOLS}:
        raise ValueError("Unknown tool")
    args = json.loads(call["arguments"])
    if selected is None:
        selected = []
    if name == "ask_clarification":
        return [clarification(catalog, args["question"], args["scope"], args["candidates"])]
    if name == "explain_no_action":
        reasons = {
            "device_not_found": "I couldn't find the named device or room in Smart Life.",
            "unsupported_control": "The device doesn't expose that control to Home Voice.",
        }
        return [Answer("Home Voice", f"No command sent. {reasons[args['reason']]}", "unavailable")]
    if name == "room_lights":
        return await room_lights(backend, catalog, args, selected)
    ids = args["device_ids"]
    if (
        not isinstance(ids, list) or not ids
        or any(not isinstance(ident, str) or ident not in catalog for ident in ids)
    ):
        raise ValueError("Unknown device")
    devices = [catalog[ident] for ident in dict.fromkeys(ids)]
    await asyncio.gather(*(hydrate_device(backend, device) for device in devices))
    code = args.get("capability")
    for device in devices:
        channel = (
            code if isinstance(code, str) and CHANNEL.fullmatch(code)
            and code in device["capabilities"] else None
        )
        selected.append((device["path"], channel))
    if name == "get_power_history":
        return await asyncio.gather(*(read_power_history(backend, device) for device in devices))
    if name == "get_device_controls":
        messages = []
        for device in devices:
            controls = ", ".join(
                label_for(device, code)
                for code, spec in device["capabilities"].items()
                if not device.get("read_only") and can_write(spec)
            )
            messages.append(Answer(device["name"], f"{controls or 'No writable controls are mapped'}."))
        return messages
    if name == "get_aliases":
        messages = []
        for device in devices:
            aliases = ", ".join(device["aliases"]) or "none"
            channels = ", ".join(
                f"{code.replace('_', ' ')}: {label}" for code, label in device["channels"].items()
            ) or ("none saved" if device["cloud_metadata"] else "metadata unavailable")
            messages.append(Answer(device["name"], f"Device names and aliases: {aliases}. Channel aliases: {channels}."))
        return messages

    code = args["capability"]
    if code is not None and (not isinstance(code, str) or not 0 < len(code) <= 128):
        raise ValueError("Invalid capability")
    if name == "get_device_status":
        return await asyncio.gather(*(read_status(
            backend, device, power_code(device) if code == "power" else code,
        ) for device in devices))
    if name == "set_light_color":
        if args["on"] is not None and type(args["on"]) is not bool:
            raise ValueError("Invalid power value")
        return await set_controls(
            backend, devices, code, args["color"], "value", color_only=True, power=args["on"],
        )
    return await set_controls(backend, devices, code, args["value"], args["format"])


async def set_controls(backend, devices, requested_code, requested_value, value_format, *,
                       color_only=False, power=None):
    writes, messages = [], []
    for device in devices:
        if device.get("read_only"):
            messages.append(Answer(device["name"], "This device is available for status only.", "unavailable"))
            continue
        code = power_code(device) if requested_code == "power" else requested_code
        spec = device["capabilities"].get(code)
        if not spec or not can_write(spec) or (color_only and spec["type"] != "object"):
            messages.append(Answer(device["name"], f"No writable {code} mapping.", "unavailable"))
            continue
        try:
            value = checked_value(spec, requested_value, value_format)
        except ValueError as error:
            messages.append(Answer(device["name"], f"{code} {error}.", "clarify"))
            continue
        values = {code: value}
        if "cloud_hsv" in spec:
            values = {
                "work_mode": "colour",
                code: {**value, **{k: round(value[k] * spec["cloud_hsv"][k] / 1000) for k in ("s", "v")}},
            }
        if power is not None:
            power_control = power_code(device)
            if not device["capabilities"].get(power_control, {}).get("writable"):
                messages.append(Answer(device["name"], "No writable power mapping.", "unavailable"))
                continue
            values = {power_control: power, **values}
        writes.append((device, code, spec, value, values))

    for device, code, spec, value, values in writes:
        path = device["path"]
        if device["cloud"]:
            url = f"{path}/commands"
            body = {"commands": [{"code": key, "value": setting} for key, setting in values.items()]}
        else:
            url, body = f"{path}/commands/capabilities", {"values": values}
        label = f"{device['name']}, {label_for(device, code)}"
        try:
            params = {"provider": "sharing"} if device["cloud"] else None
            result = result_json(await backend.post(url, json=body, params=params))
            if result.get("acknowledged") is True:
                power_label = f"power {'on' if power else 'off'} and " if power is not None else ""
                messages.append(Answer(label, f"Command accepted for {power_label}{display_value(value, spec)}.", "accepted"))
                continue
        except httpx.HTTPStatusError as error:
            messages.append(command_failure(label, error.response))
            continue
        except httpx.TimeoutException:
            messages.append(Answer(label, "Timed out waiting for a reply. The device state is unknown.", "unconfirmed"))
            continue
        except (httpx.HTTPError, ValueError):
            pass
        messages.append(Answer(label, "Could not confirm command acceptance.", "unconfirmed"))
    return messages


def command_failure(label, response):
    try:
        detail = response.json().get("detail", "")
    except (ValueError, AttributeError):
        detail = ""
    if isinstance(detail, str):
        if "код 2001" in detail:
            return Answer(label, "Tuya rejected the command because the device is offline (code 2001).", "offline")
        code = re.search(r"Tuya rejected the cloud command \(code ([0-9]{1,12})\)", detail)
        if code:
            return Answer(label, f"Tuya rejected the command (code {code[1]}).", "rejected")
        if detail.startswith("Tuya reported an error"):
            return Answer(label, "The local Tuya device did not respond successfully.", "unconfirmed")
    if response.status_code in (400, 403, 404, 409, 422):
        return Answer(label, f"The backend rejected the command (HTTP {response.status_code}).", "rejected")
    return Answer(label, f"The backend could not confirm the command (HTTP {response.status_code}).", "unconfirmed")
