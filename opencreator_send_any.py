# /// script
# requires-python = ">=3.10"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "OpenCreator Send"
# description = "Print setup for the Creator 5 / 5 Pro on OpenCreator CFW: map filaments to AFC lanes and set print start options and timelapse for one print."
# author = "OpenCreator"
# version = "0.2.1"
# ///
from dataclasses import dataclass, field
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

try:
    import orca
except ImportError:
    orca = None

MAX_JOB_BYTES = 1024 * 1024 * 1024
WINDOW_BYTES = 65536  # how much of each end of a file the firmware reads
TOOL_COUNT = 4
FRAME_COMMAND = "TIMELAPSE_TAKE_FRAME"
NOTE = "; OpenCreator Send: options for this print"
# switch, C5_PRINT_START parameter, label
OPTIONS = (
    ("flow_calibration", "FLOW_CALIBRATION", "Flow calibration"),
    ("purge", "PURGE", "Purge into bucket"),
    ("purge_line", "PURGE_LINE", "Purge line"),
    ("purge_line_all_tools", "PURGE_LINE_ALL_TOOLS", "Purge line for every tool"),
    ("bed_leveling", "BED_LEVELING", "Bed leveling"),
    ("host_pressure_advance", "HOST_PRESSURE_ADVANCE", "Host pressure advance"),
)
GROUPS = {
    "flow_calibration": "Calibration", "bed_leveling": "Calibration", "host_pressure_advance": "Calibration",
    "purge": "Purge", "purge_line": "Purge", "purge_line_all_tools": "Purge",
}
BUSY_STATES = {"printing", "paused"}


class SetupError(RuntimeError):
    pass


def log(message):
    """opencreator_send.log, next to the plugin. Orca's own log leaves this out by default."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opencreator_send.log")
        if os.path.exists(path) and os.path.getsize(path) > 1024 * 1024:
            os.remove(path)
        with open(path, "a", encoding="utf-8") as out:
            out.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def prefs_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "opencreator_send_prefs.json")


def load_prefs():
    """The options from the last send. Anything unreadable means no preferences."""
    try:
        with open(prefs_path(), encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, bool)} if isinstance(data, dict) else {}


def save_prefs(options, timelapse):
    prefs = {k: bool(v) for k, v in options.items() if isinstance(v, bool)}
    if timelapse is not None:
        prefs["timelapse"] = bool(timelapse)
    try:
        with open(prefs_path(), "w", encoding="utf-8") as out:
            json.dump(prefs, out)
    except OSError:
        pass


# --- the file


def _slot_list(key, *sources):
    for source in sources:
        found = None
        for found in re.finditer(r"^[ \t]*;[ \t]*" + key + r"[ \t]*=[ \t]*([^\r\n]*)", source, re.M):
            pass
        if found:
            values = []
            for value in found.group(1).split(","):
                try:
                    values.append(float(value))
                except ValueError:
                    values.append(0.0)
            return values
    return []


def start_command(header, footer=""):
    """The C5_PRINT_START the firmware would generate for this file. It has to stay
    in step with creator5_start_command in Kalico's virtual_sdcard.py."""
    tool = 0
    tool_seen = False
    hotend = bed = preheat = first_layer = chamber = None
    for raw in header.splitlines():
        meta = re.match(r"^\s*;\s*first_layer_height\s*=\s*([0-9.]+)", raw, re.I)
        if meta and first_layer is None:
            first_layer = float(meta.group(1))
        line = raw.split(";", 1)[0].strip()
        match = re.match(r"^T([0-3])(?:\s|$)", line, re.I)
        if match and not tool_seen:
            tool, tool_seen = int(match.group(1)), True
        match = re.match(r"^AFC_SELECT_TOOL\s+TOOL=extruder([0-3]?)\b", line, re.I)
        if match and not tool_seen:
            tool, tool_seen = int(match.group(1) or 0), True
        match = re.match(r"^M(104|109|140|190)\b.*?\bS\s*([0-9.]+)", line, re.I)
        if match and match.group(1) in ("104", "109"):
            value = float(match.group(2))
            if value > 0:
                if match.group(1) == "109" and hotend is None:
                    hotend = value
                elif match.group(1) == "104" and preheat is None:
                    preheat = value
        elif match and bed is None and match.group(1) in ("140", "190"):
            value = float(match.group(2))
            if value > 0:
                bed = value
        match = re.match(r"^M(141|191)\b.*?\bS\s*([0-9.]+)", line, re.I)
        if match and chamber is None and float(match.group(2)) > 0:
            chamber = float(match.group(2))
    if hotend is None:
        hotend = preheat
    if hotend is None:
        raise SetupError("No hotend temperature found near the start of the G-code.")
    material_header = header
    if len(header) >= WINDOW_BYTES and not header.endswith(("\n", "\r")):
        material_header = header.rpartition("\n")[0]
    materials = ()
    for source in (footer, material_header):
        found = None
        for found in re.finditer(r"^[ \t]*;[ \t]*filament_type[ \t]*=[ \t]*([^\r\n]*)", source, re.M):
            pass
        if found:
            materials = found.group(1).split(";", 4)
            break
    command = "C5_PRINT_START TOOL=%d HOTEND=%g BED=%g" % (tool, hotend, bed or 0.0)
    if first_layer is not None:
        command += " FIRST_LAYER_HEIGHT=%g" % first_layer
    for index in range(TOOL_COUNT):
        material = "UNKNOWN"
        if index < len(materials):
            value = materials[index].strip()
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1].strip()
            if re.fullmatch(r"[A-Za-z0-9+_./-]+", value):
                material = value.upper()
        command += " MATERIAL%d=%s" % (index, material)
    used = _slot_list(r"filament used \[mm\]", footer, material_header)
    tools = [index for index, value in enumerate(used[:TOOL_COUNT]) if value > 0]
    if tools:
        command += " USED_TOOLS=%s" % ",".join(map(str, tools))
    temps = _slot_list(r"nozzle_temperature_initial_layer", footer, material_header)
    for index in tools:
        if index != tool and index < len(temps) and temps[index] > 0:
            command += " HOTEND%d=%g" % (index, temps[index])
    idle = _slot_list(r"idle_temperature", footer, material_header)
    for index, value in enumerate(idle[:TOOL_COUNT]):
        if value > 0:
            command += " IDLE%d=%g" % (index, value)
    if chamber is None:
        temps = _slot_list(r"chamber_temperature", footer, material_header)
        active = _slot_list(r"activate_chamber_temp_control", footer, material_header)
        chamber = max([temps[index] for index in (tools or [tool])
                       if index < len(temps) and (index >= len(active) or active[index] > 0)] or [0.0])
    if chamber > 0:
        command += " CHAMBER=%g" % chamber
    return command


def with_parameters(command, overrides):
    for name, value in overrides.items():
        command = re.sub(r"\s+" + name + r"=\S+", "", command, flags=re.I)
        command += " %s=%d" % (name, value)
    return command


def read_ends(path):
    with open(path, "rb") as stream:
        header = stream.read(WINDOW_BYTES).decode("utf-8", "replace")
        size = os.path.getsize(path)
        offset = max(0, size - WINDOW_BYTES)
        stream.seek(max(0, offset - 1))
        at_line_start = not offset or stream.read(1) == b"\n"
        footer = stream.read(WINDOW_BYTES).decode("utf-8", "replace")
    if not at_line_start:
        footer = footer.partition("\n")[2]
    return header, footer


def _split_list(value, separator=None):
    if value is None:
        return []
    parts = value.split(separator) if separator else re.split(r"[;,]", value)
    return [part.strip().strip("\"'") for part in parts]


def _number(values, index):
    try:
        return float(values[index])
    except (IndexError, ValueError):
        return 0.0


def read_job(path, name=""):
    """What the setup window shows, from the slicer's comments in the working file."""
    if os.path.getsize(path) > MAX_JOB_BYTES:
        raise SetupError("The G-code is too large to set up.")
    stats = {}
    tool_lines = set()
    has_frames = False
    layer_marks = 0
    spelling = ""
    thumbnail, thumb_area, collecting, area = None, 0, None, 0
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line[0] not in ";Tt \t":
                continue
            text = line.strip()
            if not text:
                continue
            if text[0] != ";":
                upper = text.split(";", 1)[0].strip().upper()
                if upper == FRAME_COMMAND:
                    has_frames = True
                elif len(upper) == 2 and upper[0] == "T" and upper[1].isdigit():
                    tool_lines.add(int(upper[1]))
                else:
                    match = re.match(r"^T([0-9]+)$", upper)
                    if match:
                        tool_lines.add(int(match.group(1)))
                continue
            if collecting is not None:
                if re.match(r"^;\s*thumbnail end", text):
                    thumbnail, thumb_area, collecting = "".join(collecting), area, None
                else:
                    collecting.append(text[1:].strip())
                continue
            if "thumbnail begin" in text:
                begin = re.match(r"^;\s*thumbnail begin (\d+)[x ](\d+)", text)
                if begin:
                    area = int(begin.group(1)) * int(begin.group(2))
                    collecting = [] if area > thumb_area else None
                    continue
            if "CHANGE" in text and re.match(r"^;\s*(?:LAYER_CHANGE|CHANGE_LAYER)\s*$", text):
                layer_marks += 1
                if spelling != "LAYER_CHANGE":
                    spelling = "LAYER_CHANGE" if "LAYER_CHANGE" in text else "CHANGE_LAYER"
                continue
            if "=" in text:
                match = re.match(r"^;\s*([^=]{1,80}?)\s*=\s*(.*)$", text)
                if match:
                    stats[match.group(1)] = match.group(2).strip()
    types = _split_list(stats.get("filament_type"), ";")
    colours = _split_list(stats.get("filament_colour") or stats.get("extruder_colour"), ";")
    lengths = _split_list(stats.get("filament used [mm]"), ",")
    grams = _split_list(stats.get("filament used [g]"), ",")
    used = [i for i in range(len(lengths)) if _number(lengths, i) > 0] or sorted(tool_lines) or [0]
    tools = [{
        "tool": i,
        "material": types[i] if i < len(types) else "",
        "colour": colours[i] if i < len(colours) else "",
        "grams": _number(grams, i),
    } for i in used if i < TOOL_COUNT]
    return {
        "name": name or os.path.basename(path),
        "tools": tools,
        "time": stats.get("estimated printing time (normal mode)") or stats.get("model printing time") or "",
        "thumbnail": thumbnail,
        "has_frames": has_frames,
        "layer_marks": layer_marks,
        "layer_spelling": spelling,
    }


def rewrite_job(source, target, job, overrides, timelapse):
    """Copies source to target with the print's start options, adding (timelapse True)
    or removing (False) the layer frames. None leaves frames as they are."""
    header, footer = read_ends(source)
    explicit = re.search(r"^\s*C5_PRINT_START\b", header, re.I | re.M) is not None
    prefix = None
    if overrides and not explicit:
        prefix = NOTE + "\n" + with_parameters(start_command(header, footer), overrides) + "\n"
    mark = None
    if timelapse:
        mark = b";" + (job["layer_spelling"] or "CHANGE_LAYER").encode()
    edit_start = bool(overrides and explicit)
    frame_bytes = FRAME_COMMAND.encode()
    with open(source, "rb") as reader, open(target, "wb") as writer:
        if prefix:
            writer.write(prefix.encode())
        for line in reader:
            if not edit_start and timelapse is None:
                writer.write(line)
                shutil.copyfileobj(reader, writer, 1024 * 1024)
                break
            if line[:1] not in (b";", b"T", b"t", b"C", b"c", b" ", b"\t"):
                writer.write(line)
                continue
            stripped = line.strip()
            if edit_start and re.match(rb"^C5_PRINT_START\b", stripped, re.I):
                edit_start = False
                ending = line[len(line.rstrip(b"\r\n")):]
                line = with_parameters(stripped.decode("utf-8", "replace"), overrides).encode() + ending
            elif timelapse is False and stripped.split(b";", 1)[0].strip().upper() == frame_bytes:
                continue
            elif mark and stripped.startswith(b";") and b"CHANGE" in stripped and re.sub(rb"\s", b"", stripped) == mark:
                writer.write(frame_bytes + (b"\r\n" if line.endswith(b"\r\n") else b"\n"))
            writer.write(line)


def replace_contents(path, prepared):
    """Orca keeps using this path, so it's written over rather than swapped."""
    with open(path, "rb") as source:
        original = source.read()
    with open(prepared, "rb") as source:
        replacement = source.read()
    with open(path, "r+b") as target:
        try:
            target.write(replacement)
            target.truncate()
        except OSError:
            target.seek(0)
            target.write(original)
            target.truncate()
            raise


# --- the printer


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the API key stays with this host
        return None


class Moonraker:
    def __init__(self, address, api_key=""):
        address = address.strip().strip('"').rstrip("/")
        if "://" not in address:
            address = "http://" + address
        parts = urllib.parse.urlsplit(address)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise SetupError("The printer address isn't valid: " + address)
        self.base = address
        self.api_key = api_key.strip()
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _call(self, path, body=None, timeout=6):
        request = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET")
        if self.api_key:
            request.add_header("X-Api-Key", self.api_key)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, data, timeout=timeout) as response:
                return json.loads(response.read().decode()).get("result")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read().decode())["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise SetupError(detail or "The printer answered HTTP %d." % error.code)
        except (OSError, ValueError) as error:
            raise SetupError("Can't reach the printer: %s" % error)

    def query(self, names):
        path = "/printer/objects/query?" + "&".join(urllib.parse.quote(name) for name in names)
        return self._call(path)["status"]

    def script(self, lines):
        if lines:
            self._call("/printer/gcode/script", {"script": "\n".join(lines)}, timeout=30)

    def timelapse(self):
        try:
            return self._call("/machine/timelapse/settings")
        except SetupError:
            return None

    def queues_uploads(self):
        try:
            config = self._call("/server/config")["config"]
            return bool(config["file_manager"]["queue_gcode_uploads"])
        except (SetupError, KeyError, TypeError):
            return False

    def enable_timelapse(self):
        self._call("/machine/timelapse/settings?enabled=True&mode=layermacro", {})

    def snapshot(self):
        objects = self._call("/printer/objects/list")["objects"]
        pins = {switch: "output_pin " + switch for switch, _, _ in OPTIONS if "output_pin " + switch in objects}
        lane_objs = [obj for obj in objects if obj.startswith(("AFC_lane ", "AFC_stepper "))]
        wanted = ["webhooks", "print_stats", "idle_timeout", "AFC"]
        status = self.query([name for name in wanted if name in objects] + list(pins.values()) + lane_objs)
        afc = status.get("AFC")
        lanes = []
        if afc:
            for lane in afc.get("lanes") or []:
                obj = next((prefix + lane for prefix in ("AFC_lane ", "AFC_stepper ") if prefix + lane in status), None)
                if not obj:
                    continue
                info = status.get(obj) or {}
                maps = info.get("map") or []
                lanes.append({
                    "name": lane,
                    "extruder": info.get("extruder") or "",
                    "material": info.get("material") or "",
                    "colour": info.get("color") or "",
                    "filled": bool(info.get("prep") or info.get("load") or info.get("tool_loaded")),
                    "maps": [maps] if isinstance(maps, str) else list(maps),
                })
        return Printer(
            klippy=(status.get("webhooks") or {}).get("state", ""),
            state=(status.get("print_stats") or {}).get("state", ""),
            busy=(status.get("idle_timeout") or {}).get("state") == "Printing",
            afc=afc is not None,
            afc_error=bool(afc and afc.get("error_state")),
            bypass=bool(afc and afc.get("bypass_state")),
            share=bool(afc and afc.get("multiple_tool_mapping")),
            lanes=lanes,
            switches={switch: float((status.get(obj) or {}).get("value", 0)) >= 0.5
                      for switch, obj in pins.items()},
            timelapse=self.timelapse(),
            queue_uploads=self.queues_uploads(),
        )


@dataclass
class Printer:
    """What the printer reported at one moment, and the questions the setup asks of it."""
    klippy: str = "ready"
    state: str = ""
    busy: bool = False
    afc: bool = False
    afc_error: bool = False
    bypass: bool = False
    share: bool = False
    lanes: list = field(default_factory=list)
    switches: dict = field(default_factory=dict)
    timelapse: dict = None
    queue_uploads: bool = False

    def printing(self):
        return self.state in BUSY_STATES or self.busy

    def blocked(self):
        if self.klippy != "ready":
            return "Klipper isn't ready."
        if self.printing():
            return "The printer is busy."
        if self.afc_error:
            return "AFC has an error. Clear it on the printer first."
        return ""

    def lane_for_tool(self, tool):
        for lane in self.lanes:
            if "T%d" % tool in lane["maps"]:
                return lane["name"]
        return None

    def current_mapping(self, tools):
        return {str(t["tool"]): self.lane_for_tool(t["tool"]) for t in tools}

    @staticmethod
    def _rgb(colour):
        text = (colour or "").lstrip("#")
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        try:
            return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None

    @classmethod
    def _distance(cls, a, b):
        a, b = cls._rgb(a), cls._rgb(b)
        if a is None or b is None:
            return 500.0
        return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5

    @staticmethod
    def _same_material(a, b):
        family = lambda value: re.sub(r"[^A-Z0-9]", "", (value or "").upper())
        return family(a) == family(b)

    def auto_mapping(self, tools):
        """The closest loaded lane for each tool: the material first, then the colour."""
        free = [lane for lane in self.lanes if lane["filled"]]
        mapping = self.current_mapping(tools)
        for tool in tools:
            if not free:
                break
            tag = "T%d" % tool["tool"]
            best = min(free, key=lambda lane: (not self._same_material(tool["material"], lane["material"]),
                                               self._distance(tool["colour"], lane["colour"]),
                                               tag not in lane["maps"]))
            mapping[str(tool["tool"])] = best["name"]
            if not self.share:
                free.remove(best)
        return mapping

    def plan_mapping(self, tools, mapping):
        """SET_MAP commands to reach the mapping, and anything that blocks the print."""
        if not self.afc or self.bypass:
            return [], []
        lanes = {lane["name"]: lane for lane in self.lanes}
        errors = []
        chosen = {}
        for tool in tools:
            name = mapping.get(str(tool["tool"]))
            if name not in lanes:
                errors.append("T%d has no lane." % tool["tool"])
            elif not lanes[name]["filled"]:
                errors.append("T%d: %s has no filament." % (tool["tool"], name))
            elif name in chosen and not self.share:
                errors.append("%s can't feed both T%d and T%d." % (name, chosen[name], tool["tool"]))
            chosen.setdefault(name, tool["tool"])
        if errors:
            return [], errors
        # SET_MAP swaps when a lane can only hold one tool, so each step sees the last
        state = {lane["name"]: list(lane["maps"]) for lane in self.lanes}
        lines = []
        for tool in tools:
            wanted = mapping[str(tool["tool"])]
            tag = "T%d" % tool["tool"]
            if tag in state[wanted]:
                continue
            lines.append("SET_MAP LANE=%s MAP=%s" % (wanted, tag))
            holder = next((name for name, maps in state.items() if tag in maps), None)
            if holder and not self.share:
                state[holder] = list(state[wanted])
                state[wanted] = [tag]
            else:
                if holder:
                    state[holder].remove(tag)
                state[wanted].append(tag)
        return lines, []

    def initial_mapping(self, tools):
        """The closest lanes, unless that can't be sent, then what the printer has."""
        mapping = self.auto_mapping(tools)
        return mapping if not self.plan_mapping(tools, mapping)[1] else self.current_mapping(tools)


def job_overrides(printer, options):
    """Only what differs from the printer's switches, plus the one option without one."""
    overrides = {}
    for switch, parameter, _ in OPTIONS:
        if switch in printer.switches and switch in options and bool(options[switch]) != printer.switches[switch]:
            overrides[parameter] = 1 if options[switch] else 0
    if options.get("buildplate_check") is False:
        overrides["BUILDPLATE_CHECK"] = 0
    return overrides


def can_timelapse(job):
    return bool(job["has_frames"] or job["layer_marks"])


def plan_setup(printer, job, request):
    """Pure: what apply_setup must do, as (lines, overrides, timelapse edit True/False/None,
    whether to turn the printer's timelapse on). Raises SetupError when it can't be done."""
    reason = printer.blocked()
    if reason:
        raise SetupError(reason)
    lines, errors = printer.plan_mapping(job["tools"], request.get("mapping") or {})
    if errors:
        raise SetupError(" ".join(errors))
    overrides = job_overrides(printer, request.get("options") or {})
    wanted = request.get("timelapse")
    settings = printer.timelapse
    timelapse = None
    if settings is not None and wanted is not None:
        if wanted and not job["has_frames"]:
            if not can_timelapse(job):
                raise SetupError("This file has no layer change markers for a timelapse.")
            timelapse = True
        elif not wanted and job["has_frames"]:
            timelapse = False
    enable = settings is not None and bool(wanted) and not (settings.get("enabled") and settings.get("mode") == "layermacro")
    return lines, overrides, timelapse, enable


def apply_setup(client, job, path, request):
    """Everything on the printer goes first, and the file changes last, so a failure
    leaves the job as Orca made it. Map changes stay on the printer, as they do from
    the printer's own mapping screen."""
    lines, overrides, timelapse, enable = plan_setup(client.snapshot(), job, request)
    tools = job["tools"]
    mapping = request.get("mapping") or {}
    prepared = None
    try:
        if overrides or timelapse is not None:
            handle, prepared = tempfile.mkstemp(suffix=".gcode", dir=os.path.dirname(path) or None)
            os.close(handle)
            rewrite_job(path, prepared, job, overrides, timelapse)
        if enable:
            client.enable_timelapse()
        client.script(lines)
        if lines:
            after = client.snapshot()
            for tool in tools:
                if after.lane_for_tool(tool["tool"]) != mapping[str(tool["tool"])]:
                    raise SetupError("The printer didn't take the mapping for T%d. Nothing was changed in the file." % tool["tool"])
        if prepared:
            replace_contents(path, prepared)
        save_prefs(request.get("options") or {}, request.get("timelapse"))
    finally:
        if prepared and os.path.exists(prepared):
            os.remove(prepared)


def view_state(printer, job):
    settings = printer.timelapse
    prefs = load_prefs()
    timelapse_on = bool(job["has_frames"] and settings.get("enabled")) if settings else False
    if settings and "timelapse" in prefs and can_timelapse(job):
        timelapse_on = prefs["timelapse"]
    return {
        "kind": "state",
        "blocked": printer.blocked(),
        "printing": printer.printing(),
        "queue_uploads": printer.queue_uploads,
        "state": printer.state,
        "afc": printer.afc,
        "bypass": printer.bypass,
        "share": printer.share,
        "lanes": printer.lanes,
        "mapping": printer.initial_mapping(job["tools"]),
        "buildplate_check": prefs.get("buildplate_check", True),
        "current": printer.current_mapping(job["tools"]),
        "options": [{"id": switch, "label": label, "group": GROUPS[switch], "printer_on": printer.switches[switch],
                     "on": prefs.get(switch, printer.switches[switch])}
                    for switch, _, label in OPTIONS if switch in printer.switches],
        "timelapse": None if settings is None else {
            "on": timelapse_on,
            "enabled": bool(settings.get("enabled")),
            "mode": settings.get("mode") or "",
        },
    }


# --- the window

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}
:root{--r:5px;--accent:var(--orca-accent,#2a8a4a)}
html,body{height:100%;margin:0}
body{padding:14px 16px;display:flex;flex-direction:column;gap:10px;overflow:hidden;font:14px/1.4 system-ui,-apple-system,sans-serif;background:var(--orca-bg,#fff);color:var(--orca-fg,#222)}
.main{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:10px}
h1{font-size:16px;margin:0 0 2px;overflow-wrap:anywhere;line-height:1.25}
h2{font-size:13px;margin:0 0 8px;display:flex;align-items:center;gap:8px;color:inherit;min-height:26px}
h2>span:first-child{font-weight:600}h2 .gap{flex:1}
h3{font-size:12px;margin:0 0 4px;opacity:.65;font-weight:600}
.card{border:1px solid #8884;border-radius:var(--r);padding:12px;background:#8881;flex-shrink:0}
.top{display:flex;gap:12px;align-items:center}
.top img{width:72px;height:72px;object-fit:contain;border-radius:var(--r);border:1px solid #8884;background:#8881;flex-shrink:0}
.top>div{flex:1;min-width:0}
.bar{display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap}
.dim{opacity:.7;font-size:12.5px;font-weight:400}
table{width:100%;border-collapse:collapse}
tr+tr td{border-top:1px solid #8882}
td{padding:7px 4px;vertical-align:top}
.cell{min-height:30px;display:flex;align-items:center;gap:8px}
td.tool{white-space:nowrap;font-weight:500;padding-left:9px}
tr.r-warn td.tool{box-shadow:inset 3px 0 0 #c77700}tr.r-bad td.tool{box-shadow:inset 3px 0 0 #d03030}
td.grams{white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums;padding-right:8px}
td.grams .cell{justify-content:flex-end}
td.arr{opacity:.45;text-align:center;width:20px}
td.arr .cell{justify-content:center}
td.lane{width:100%}
.dot{display:inline-block;width:16px;height:16px;border-radius:var(--r);border:1px solid #8888;flex-shrink:0}
.pick{min-height:30px;display:flex;align-items:center;gap:8px}
select{flex:1;min-width:0;height:30px;padding:0 8px;border:1px solid #8885;border-radius:var(--r);background:var(--orca-bg,#fff);color:inherit;font:inherit}
.note{font-size:12.5px;margin-top:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px 18px}
.mon{margin-top:10px;padding-top:10px;border-top:1px solid #8882}
label{display:flex;align-items:center;gap:8px;padding:3px 0;cursor:pointer;user-select:none;min-height:28px}
label{position:relative}
input[type=checkbox]{position:absolute;opacity:0;width:18px;height:18px;margin:0;pointer-events:none}
input[type=checkbox]+span::before{content:"";display:inline-block;box-sizing:border-box;width:18px;height:18px;margin-right:8px;vertical-align:-4px;border:2px solid #888;border-color:color-mix(in srgb,currentColor 55%,transparent);border-radius:var(--r);background:center/12px no-repeat}
input[type=checkbox]+span{white-space:nowrap;flex-shrink:0}
label:hover input:not(:checked)+span::before{border-color:var(--accent)}
input[type=checkbox]:checked+span::before{background-color:var(--accent);border-color:var(--accent);background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 6.5l3 3 5-6' fill='none' stroke='white' stroke-width='2'/%3E%3C/svg%3E")}
input[type=checkbox]:focus-visible+span::before{outline:2px solid var(--accent);outline-offset:2px}
label.off{opacity:.45}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.warn{color:#c77700}.bad{color:#d03030}.ok{color:#2a8a4a}
.pill{display:inline-block;padding:2px 10px;border-radius:var(--r);font-size:12.5px;font-weight:600;color:#fff;background:#888}
.pill.ok{background:#2a8a4a}.pill.warn{background:#c77700}.pill.bad{background:#d03030}
.tag{font-size:11.5px;padding:0 6px;border-radius:var(--r);background:#8883;white-space:nowrap;font-weight:400}
#message{font-size:12.5px;flex-shrink:0}
#message:empty{display:none}
#message>div{padding:8px 12px;border-radius:var(--r);border:1px solid #8884;background:#8881}
#message>div+div{margin-top:6px}
#message>div.bad{border-color:#d0303066;background:#d0303018}
#message>div.warn{border-color:#c7770066;background:#c7770018}
.feet{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;align-items:center;padding-top:10px;border-top:1px solid #8883;flex-shrink:0}
.feet .gap{flex:1}
button{padding:7px 14px;cursor:pointer;font:inherit;border-radius:var(--r);border:1px solid #8886;background:#8882;color:inherit}
button:hover:not(:disabled){background:#8883}
button.main{background:var(--accent);color:#fff;border-color:transparent;font-weight:600;padding:7px 18px}
button.main:hover:not(:disabled){opacity:.92}
button.cancel{border-color:#d0303088;background:#d0303022;color:#d84545}
button.cancel:hover:not(:disabled){background:#d0303038}
button.small{padding:3px 10px;font-size:12.5px}
button:disabled{opacity:.45;cursor:default}
</style></head><body>
<div class="main">
<div class="card top"><img id="thumb" hidden alt=""><div><h1 id="name"></h1><div class="dim" id="facts"></div>
<div class="bar"><span class="pill" id="pill">Reading...</span><button class="small" id="refresh" title="Read the printer again">Refresh</button></div></div></div>
<div class="card" id="mapbox" hidden><h2><span>Filament to lane</span><span class="gap"></span><button class="small" id="current" hidden title="Use the printer's current tool mapping">Current</button><button class="small" id="auto" title="Match each tool by material and colour">Auto match</button></h2><table id="map"></table></div>
<div class="card"><h2><span>Print options</span><span class="dim">this print only</span><span class="gap"></span><button class="small" id="reset" disabled title="Reset options to match the printer's switches">Reset to printer</button></h2>
<div class="grid" id="options"></div>
<div class="mon"><h3>Monitoring</h3>
<div class="grid">
<label id="plate"><input type="checkbox" id="buildplate_check"><span>Build plate check</span><span class="tag" id="plate_tag" hidden title="Turned off for this print">off</span></label>
<label id="tl" hidden><input type="checkbox" id="timelapse"><span>Timelapse</span><span class="dim" id="tlnote"></span></label></div></div></div>
</div>
<div id="message"></div>
<div class="feet"><button class="cancel" id="cancel">Cancel send</button><span class="gap"></span><button id="skip">Send unchanged</button>
<button id="apply" class="main" disabled>Apply &amp; send</button></div>
<script>
const job=__JOB__;const $=id=>document.getElementById(id);let view=null,mapping={},auto={},busy=false,applying=false,busyText='',rows=[];
const send=m=>window.orca.postMessage(m);
const text=(tag,value,cls)=>{const e=document.createElement(tag);e.textContent=value;if(cls)e.className=cls;return e};
const hex=c=>/^#?[0-9a-f]{6}$/i.test(c||'')?(c[0]=='#'?c:'#'+c):null;
const hasColour=c=>/^#?[0-9a-f]{3,8}$/i.test(c||'');
const dot=c=>{const s=document.createElement('span');s.className='dot';if(hasColour(c)){s.style.background=c[0]=='#'?c:'#'+c;s.title=c}return s};
const rgb=c=>{const h=hex(c);return h?[1,3,5].map(i=>parseInt(h.substr(i,2),16)):null};
const dist=(a,b)=>{a=rgb(a);b=rgb(b);return a&&b?Math.hypot(a[0]-b[0],a[1]-b[1],a[2]-b[2]):0};
const fam=s=>(s||'').replace(/\W/g,'').toUpperCase();
const mapped=()=>view&&view.afc&&!view.bypass&&job.tools.length>0;
const laneOf=t=>view.lanes.find(x=>x.name==mapping[t.tool]);
const totalGrams=job.tools.reduce((s,t)=>s+Math.round((t.grams||0)*10),0)/10;
$('name').textContent=job.name;
$('facts').textContent=[job.time,totalGrams>0?totalGrams.toFixed(1)+' g':'',job.tools.map(t=>'T'+t.tool+(t.material?' '+t.material:'')).join(', ')].filter(Boolean).join(' · ');
if(job.thumbnail){$('thumb').src='data:image/png;base64,'+job.thumbnail;$('thumb').hidden=false}
function rowIssue(t){const l=laneOf(t);if(!l)return['bad','No lane chosen.'];if(!l.filled)return['bad',l.name+' has no filament.'];
 if(!view.share){const o=job.tools.find(x=>x!==t&&mapping[x.tool]==l.name);if(o)return['bad',l.name+' is also used by T'+o.tool+'.']}
 if(l.material&&t.material&&fam(l.material)!=fam(t.material))return['warn','Sliced for '+t.material+', '+l.name+' has '+l.material+'.'];
 if(dist(t.colour,l.colour)>120)return['warn','Colour is far from the sliced colour.'];return['ok','']}
function problems(){if(!view)return['-'];const out=[];if(view.blocked)out.push(view.blocked);
 if(mapped())for(const t of job.tools){const i=rowIssue(t);if(i[0]=='bad')out.push('T'+t.tool+': '+i[1])}return out}
function laneLabel(x){const ext=x.extruder&&x.extruder!=x.name?' ('+x.extruder+')':'';return x.name+ext+' — '+(x.filled?x.material||'unset':'empty')}
function buildMap(){const table=$('map');table.textContent='';rows=[];for(const t of job.tools){const r=table.insertRow();
 const a=r.insertCell(),ac=text('div','','cell');a.className='tool';ac.append(dot(t.colour),text('span','T'+t.tool+(t.material?' '+t.material:'')));a.append(ac);
 const g=r.insertCell();g.className='grams dim';g.append(text('div',t.grams?t.grams.toFixed(1)+' g':'','cell'));
 const ar=r.insertCell();ar.className='arr';ar.append(text('div','→','cell'));
 const c=r.insertCell(),box=document.createElement('div'),ld=dot(''),s=document.createElement('select'),tg=text('span','','tag'),nt=text('div','','note');
 c.className='lane';box.className='pick';ld.hidden=true;tg.hidden=true;nt.hidden=true;
 s.onchange=()=>choose(t.tool,s.value);box.append(ld,s,tg);c.append(box,nt);
 rows.push({t,tr:r,s,ld,tg,nt})}}
function syncMapOptions(){if(!rows.length)buildMap();for(const r of rows){r.s.textContent='';
 for(const x of view.lanes){const o=document.createElement('option');o.value=x.name;o.disabled=!x.filled;o.textContent=laneLabel(x);r.s.append(o)}}}
function drawMap(){if(!rows.length)syncMapOptions();let remapped=false;for(const r of rows){const t=r.t,l=laneOf(t),cur=view.current&&view.current[t.tool];
 if(cur&&mapping[t.tool]!=cur)remapped=true;
 r.s.value=mapping[t.tool]||'';r.s.disabled=busy;
 if(l&&hasColour(l.colour)){r.ld.style.background=l.colour[0]=='#'?l.colour:'#'+l.colour;r.ld.title=l.colour;r.ld.hidden=false}else{r.ld.hidden=true}
 if(cur&&mapping[t.tool]!=cur){r.tg.textContent=auto[t.tool]?'auto':'remap';r.tg.title='Printer currently maps T'+t.tool+' to '+cur;r.tg.hidden=false}else{r.tg.hidden=true}
 const i=rowIssue(t);
 if(i[1]){r.nt.textContent=i[1];r.nt.className='note '+i[0];r.nt.hidden=false}else{r.nt.hidden=true}
 r.tr.className='r-'+i[0];r.tr.title=i[1]||'Ready'}
 $('current').hidden=!remapped}
function choose(tool,lane){if(!view.share)for(const k in mapping)if(k!=tool&&mapping[k]==lane)mapping[k]=mapping[tool];mapping[tool]=lane;delete auto[tool];draw()}
function pill(){const p=$('pill');let c='ok',t='Ready';if(view.blocked){c='bad';t='Not ready'}else if(view.printing){c='warn';t='Printing'}p.className='pill '+c;p.textContent=t;p.title='Printer state: '+(view.state||'unknown')}
function syncOptionsUI(){if(!view)return;let diff=!$('buildplate_check').checked;
 $('plate_tag').hidden=$('buildplate_check').checked;
 const pl=document.querySelector('[data-option="purge_line"]'),pla=document.querySelector('[data-option="purge_line_all_tools"]');
 if(pl&&pla)pla.closest('label').classList.toggle('off',!pl.checked);
 document.querySelectorAll('[data-option]').forEach(i=>{if(i.checked!=(i.dataset.printer=='1'))diff=true});
 $('reset').disabled=busy||!diff}
function draw(){if(!view)return;$('mapbox').hidden=!mapped();if(!$('mapbox').hidden)drawMap();pill();syncOptionsUI();
 const bad=view.blocked?[view.blocked]:[],m=$('message');m.textContent='';if(bad.length)m.append(text('div',bad.join(' '),'bad'));
 if(view.printing)m.append(text('div',view.queue_uploads?'The printer is printing. With Upload and Print, Moonraker adds this file to its print queue.':'The printer is printing, and Moonraker has queue_gcode_uploads off, so this file will be stored but not queued or printed.','warn'));
 if(busy&&busyText)m.append(text('div',busyText,'dim'));
 $('apply').disabled=busy||problems().length>0;$('apply').textContent=applying?'Applying…':'Apply & send';for(const id of ['refresh','auto','current','cancel','skip'])$(id).disabled=busy}
function option(o){const l=document.createElement('label'),i=document.createElement('input'),b=text('span','printer '+(o.printer_on?'on':'off'),'tag');
 i.type='checkbox';i.dataset.option=o.id;i.dataset.printer=o.printer_on?'1':'0';i.checked=o.on;
 b.hidden=o.on==o.printer_on;b.title='Overrides the printer\'s switch for this print';
 i.onchange=()=>{b.hidden=i.checked==o.printer_on;syncOptionsUI()};l.append(i,text('span',o.label),b);return l}
function load(state){view=state;mapping=Object.assign({},state.mapping);auto={};
 for(const t of job.tools)if(mapping[t.tool]!=state.current[t.tool])auto[t.tool]=1;
 if(mapped())syncMapOptions();
 $('buildplate_check').checked=state.buildplate_check;const box=$('options');box.textContent='';const groups={};
 for(const o of state.options){if(!groups[o.group]){const g=document.createElement('div');g.append(text('h3',o.group));groups[o.group]=g;box.append(g)}groups[o.group].append(option(o))}
 const t=state.timelapse;$('tl').hidden=!t;if(t){$('timelapse').checked=t.on;
  $('tlnote').textContent=!t.enabled||t.mode!='layermacro'?'(turns on layer timelapse on the printer)':''}
 draw()}
function options(){const out={buildplate_check:$('buildplate_check').checked};document.querySelectorAll('[data-option]').forEach(i=>out[i.dataset.option]=i.checked);return out}
function apply(){if($('apply').disabled)return;busy=true;applying=true;busyText='Updating lanes and preparing the file...';draw();send({action:'apply',mapping,options:options(),timelapse:view.timelapse?$('timelapse').checked:null})}
window.orca.onMessage(d=>{busy=false;applying=false;if(d.kind=='state')load(d);else if(d.kind=='mapping'){mapping=d.mapping;auto={};for(const t of job.tools)if(!view||!view.current||mapping[t.tool]!=view.current[t.tool])auto[t.tool]=1;draw()}
 else if(d.kind=='error'){if(!view){$('pill').textContent='unavailable';$('pill').className='pill bad'}$('message').textContent='';$('message').append(text('div',d.message,'bad'));$('apply').disabled=!view;for(const id of ['refresh','auto','current','cancel','skip'])$(id).disabled=false;syncOptionsUI()}});
$('refresh').onclick=()=>{busy=true;busyText='Reading the printer...';draw();send({action:'read'})};$('auto').onclick=()=>{send({action:'auto'})};
$('current').onclick=()=>{if(view&&view.current){mapping=Object.assign({},view.current);auto={};draw()}};
$('reset').onclick=()=>{if(!view)return;$('buildplate_check').checked=true;document.querySelectorAll('[data-option]').forEach(i=>{i.checked=i.dataset.printer=='1';i.onchange()});syncOptionsUI()};
$('buildplate_check').onchange=syncOptionsUI;
$('cancel').onclick=()=>send({action:'cancel'});$('skip').onclick=()=>send({action:'skip'});$('apply').onclick=apply;
document.addEventListener('keydown',e=>{if(e.key=='Escape'&&!busy)$('cancel').click();
 else if(e.key=='Enter'&&!['BUTTON','SELECT'].includes(e.target.tagName))apply()});
send({action:'read'});
</script></body></html>"""


def page_html(job):
    return PAGE.replace("__JOB__", json.dumps(job).replace("<", "\\u003c"))


USE_3MF_NOTE = ("This print is being sent unchanged, without the setup window, because the printer "
                "profile has \"Use 3MF instead of G-code\" on.\n\nTurn it off under Printer settings > "
                "Basic information, save the profile, then slice and send again.")


def is_3mf_send(path, output_name):
    """With Use 3MF on, Orca reports a send as a File export, but its target is a temp
    bundle beside the working G-code. A real export goes where the user chose."""
    if not output_name or not str(output_name).lower().endswith(".3mf"):
        return False
    return os.path.normcase(os.path.dirname(os.path.abspath(path))) == \
        os.path.normcase(os.path.dirname(os.path.abspath(str(output_name))))


def is_creator5(config_value):
    identity = "%s %s" % (config_value("printer_model") or "", config_value("printer_settings_id") or "")
    return re.search(r"creator\s*5", identity, re.I) is not None


def printer_address(config_value):
    address = str(config_value("print_host") or "").strip().strip('"')
    port = str(config_value("printhost_port") or "").strip().strip('"')
    if address and port:
        parts = urllib.parse.urlsplit(address if "://" in address else "http://" + address)
        if parts.port is None:
            address = urllib.parse.urlunsplit((parts.scheme, parts.netloc + ":" + port, parts.path, parts.query, parts.fragment))
    return address


class SetupSession:
    """One window: its messages arrive on the UI thread, so printer calls get a thread."""

    def __init__(self, client, job, path):
        self.client = client
        self.job = job
        self.path = path
        self.window = None
        self.done = threading.Event()
        self.result = "cancel"
        self.working = threading.Lock()
        self.last_snapshot = None

    def post(self, payload):
        if self.window is not None and not self.done.is_set():
            self.window.post(payload)

    def finish(self, result):
        self.result = result
        self.done.set()

    def on_message(self, message):
        action = message.get("action") if isinstance(message, dict) else None
        if action == "cancel":
            self.finish("cancel")
        elif action == "skip":
            self.finish("continue")
        elif action == "auto":
            if self.last_snapshot:
                self.post({"kind": "mapping", "mapping": self.last_snapshot.auto_mapping(self.job["tools"])})
        elif action in ("read", "apply"):
            threading.Thread(target=self._work, args=(message,), daemon=True).start()

    def _work(self, message):
        if not self.working.acquire(blocking=False):
            return
        try:
            if message["action"] == "read":
                self.last_snapshot = self.client.snapshot()
                self.post(view_state(self.last_snapshot, self.job))
            else:
                apply_setup(self.client, self.job, self.path, message)
                self.finish("continue")
        except (SetupError, OSError, ValueError, KeyError, TypeError) as error:
            self.post({"kind": "error", "message": str(error)})
        finally:
            self.working.release()


if orca is not None:
    class OpenCreatorSend(orca.slicing.SlicingPipelineCapabilityBase):
        def get_name(self):
            return "OpenCreator Send"

        def on_load(self):
            log("loaded")

        def execute(self, ctx):
            if ctx.step != orca.slicing.Step.psGCodePostProcess:
                return orca.ExecutionResult.skipped()
            get = ctx.config_value
            path = os.fspath(ctx.gcode_path) if ctx.gcode_path else ""
            address = printer_address(get)
            log("post-process: host=%r model=%r settings=%r address=%r file=%r" % (
                ctx.host, get("printer_model"), get("printer_settings_id"), address, path))
            if not is_creator5(get):
                return orca.ExecutionResult.skipped("Not a Creator 5 profile.")
            if str(ctx.host or "File").strip().lower() == "file":
                if is_3mf_send(path, ctx.output_name):
                    # Orca runs this send on the UI thread, where waiting for a window hangs it
                    orca.host.ui.message(USE_3MF_NOTE, "OpenCreator Send")
                return orca.ExecutionResult.skipped("File export: no printer.")
            if not path or not os.path.isfile(path) or not address:
                return orca.ExecutionResult.skipped("No working file or printer address.")
            try:
                job = read_job(path, ctx.output_name or "")
                session = SetupSession(Moonraker(address, str(get("printhost_apikey") or "")), job, path)
                session.window = orca.host.ui.create_window(
                    html=page_html(job), title="OpenCreator Send", width=640, height=680,
                    on_message=session.on_message, on_close=lambda: session.finish(session.result))
            except Exception as error:  # a setup problem must not stop an ordinary send
                log(traceback.format_exc())
                try:
                    orca.host.ui.message("Setup couldn't open, so this print is being sent unchanged.\n\n%s" % error,
                                         "OpenCreator Send")
                except Exception:
                    log(traceback.format_exc())
                return orca.ExecutionResult.success("Setup unavailable (%s). Sending unchanged." % error)
            log("window open")
            try:
                while not session.done.wait(0.15):
                    if ctx.cancelled():
                        session.finish("cancel")
            finally:
                if session.window.is_open():
                    session.window.close()
                with session.working:  # a file edit in progress finishes first
                    pass
            log("window closed: " + session.result)
            if session.result == "cancel":
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, "Send cancelled.")
            return orca.ExecutionResult.success("Print setup done.")

    @orca.plugin
    class OpenCreatorSendPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(OpenCreatorSend)
