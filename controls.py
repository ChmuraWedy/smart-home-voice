"""Build the voice tool catalog from registered devices and their API definitions."""

import asyncio
import json
import math
import re
from urllib.parse import quote

import httpx

CHANNEL = re.compile(r"switch_[1-9][0-9]*")
SCALAR_TYPES = {"boolean", "integer", "number", "string"}


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
TOOLS = [
    tool("get_device_status", "Read current device or channel state; use null for all readings.", {
        **TARGETS, **CAPABILITY,
    }),
    tool("get_aliases", "List device aliases and named channels for the specified devices.", TARGETS),
    tool("get_device_controls", "List the available writable controls for the specified devices.", TARGETS),
    tool("set_device_control", "Set one advertised writable capability on the selected devices.", {
        **TARGETS,
        "capability": {"type": "string"},
        "value": {"anyOf": [
            {"type": "boolean"}, {"type": "number"}, {"type": "string"},
            {
                "type": "object",
                "properties": {key: {"type": "integer"} for key in ("h", "s", "v")},
                "required": ["h", "s", "v"], "additionalProperties": False,
            },
        ]},
        "format": {"type": "string", "enum": ["value", "percent"]},
    }),
]


def result_json(response):
    response.raise_for_status()
    return response.json()


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
    return result


async def load_catalog(backend, labels, cloud_ids):
    rows = result_json(await backend.get("devices"))
    try:
        cloud = {row["id"]: row for row in result_json(await backend.get(
            "cloud/devices", params={"provider": "sharing"}, timeout=4,
        ))}
    except (httpx.HTTPError, ValueError):
        cloud = {}

    catalog = {}
    for row in rows:
        ident = str(row["id"])
        if ident not in labels or not row["enabled"]:
            continue
        remote = cloud.get(row["device_id"])
        definitions = remote["controls"]["definitions"] if remote else {}
        channels = {
            spec["code"]: spec["user_label"]
            for spec in definitions.get("functions", []) + definitions.get("status_range", [])
            if CHANNEL.fullmatch(spec["code"]) and spec.get("user_label")
        }
        use_cloud = ident in cloud_ids
        capabilities = cloud_controls(remote) if use_cloud and remote else (
            {} if use_cloud else row["capabilities"]
        )
        catalog[ident] = {
            "name": labels[ident], "aliases": sorted(set(row["aliases"]) | {row["name"]}
                                                  | set(remote.get("aliases", []) if remote else [])),
            "channels": channels, "capabilities": capabilities,
            "path": f"cloud/devices/{quote(row['device_id'], safe='')}" if use_cloud else f"devices/{ident}",
            "cloud": use_cloud, "cloud_metadata": remote is not None,
        }
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
            "controls": {
                code: {
                    **{field: spec[field] for field in fields if spec.get(field) is not None},
                    "writable": can_write(spec),
                }
                for code, spec in device["capabilities"].items()
            },
        }
        for ident, device in catalog.items()
    }


def label_for(device, code):
    return device["channels"].get(code) or code.replace("_", " ")


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
        return f"{name} has no readable {code} mapping."
    if not specs:
        return f"{name} has no status mapping for its configured connection."
    try:
        if device["cloud"]:
            response = result_json(await backend.get(
                f"{path}/status", params={"refresh": "true", "provider": "sharing"},
            ))
        else:
            response = result_json(await backend.get(f"{path}/state", params={"refresh": "true"}))
        if device["cloud"]:
            if not response["online"]:
                return f"{name} is offline in Tuya Sharing."
            readings = response["status"]
        else:
            readings = {
                key: reading["value"] for key, reading in response["values"].items()
                if reading["known"] and not reading["stale"]
            }
    except (httpx.HTTPError, ValueError, KeyError):
        return f"Could not refresh {name}."
    values = [
        f"{label_for(device, key)}: {display_value(value, specs[key])}"
        for key, value in readings.items()
        if key in specs and specs[key]["readable"] and (code is None or key == code)
    ]
    return f"{name}: {', '.join(values)}." if values else f"No current reading for {name}."


async def execute(backend, catalog, call):
    name = call["name"]
    if name not in {tool["name"] for tool in TOOLS}:
        raise ValueError("Unknown tool")
    args = json.loads(call["arguments"])
    ids = args["device_ids"]
    if (
        not isinstance(ids, list) or not ids
        or any(not isinstance(ident, str) or ident not in catalog for ident in ids)
    ):
        raise ValueError("Unknown device")
    devices = [catalog[ident] for ident in dict.fromkeys(ids)]
    if name == "get_device_controls":
        messages = []
        for device in devices:
            controls = ", ".join(
                label_for(device, code)
                for code, spec in device["capabilities"].items() if can_write(spec)
            )
            messages.append(f"{device['name']}: {controls or 'no writable controls are mapped'}.")
        return messages
    if name == "get_aliases":
        messages = []
        for device in devices:
            aliases = ", ".join(device["aliases"]) or "none"
            channels = ", ".join(
                f"{code.replace('_', ' ')}: {label}" for code, label in device["channels"].items()
            ) or ("none saved" if device["cloud_metadata"] else "metadata unavailable")
            messages.append(f"{device['name']}. Device names and aliases: {aliases}. Channel aliases: {channels}.")
        return messages

    code = args["capability"]
    if code is not None and (not isinstance(code, str) or not 0 < len(code) <= 128):
        raise ValueError("Invalid capability")
    if name == "get_device_status":
        return await asyncio.gather(*(read_status(backend, device, code) for device in devices))

    writes, messages = [], []
    for device in devices:
        spec = device["capabilities"].get(code)
        if not spec or not can_write(spec):
            messages.append(f"{device['name']} has no writable {code} mapping.")
            continue
        try:
            value = checked_value(spec, args["value"], args["format"])
        except ValueError as error:
            messages.append(f"{device['name']}: {code} {error}.")
            continue
        writes.append((device, spec, value))

    for device, spec, value in writes:
        path = device["path"]
        if device["cloud"]:
            url, body = f"{path}/commands", {"commands": [{"code": code, "value": value}]}
        else:
            url, body = f"{path}/commands/capabilities", {"values": {code: value}}
        label = f"{device['name']}, {label_for(device, code)}"
        try:
            params = {"provider": "sharing"} if device["cloud"] else None
            result = result_json(await backend.post(url, json=body, params=params))
            if result.get("acknowledged") is True:
                messages.append(f"{label}: command accepted for {display_value(value, spec)}.")
                continue
        except httpx.HTTPStatusError as error:
            if error.response.status_code in (400, 403, 404, 409, 422):
                messages.append(f"{label}: command rejected by the backend.")
                continue
        except (httpx.HTTPError, ValueError):
            pass
        messages.append(f"{label}: could not confirm command acceptance.")
    return messages
