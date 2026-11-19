"""The plugin's Orca entry point against a stand-in for the orca module."""
import os
import sys
import tempfile
import threading
import types

orca = types.ModuleType("orca")
results = types.SimpleNamespace(success=lambda m="": ("success", m), skipped=lambda m="": ("skipped", m),
                                failure=lambda status, m, data="": ("failure", m))
orca.ExecutionResult = results
orca.PluginResult = types.SimpleNamespace(RecoverableError="recoverable")
orca.slicing = types.SimpleNamespace(SlicingPipelineCapabilityBase=object, Step=types.SimpleNamespace(psGCodePostProcess="post", other="other"))
registered = []
orca.register_capability = registered.append
orca.base = object
orca.plugin = lambda cls: cls


class Window:
    def __init__(self, action, **kwargs):
        self.open = True
        self.on_close = kwargs["on_close"]
        self.posts = []
        threading.Timer(0.05, lambda: kwargs["on_message"]({"action": action})).start()

    def post(self, payload):
        self.posts.append(payload)

    def is_open(self):
        return self.open

    def close(self):
        self.open = False


action = {"value": "cancel"}
orca.host = types.SimpleNamespace(ui=types.SimpleNamespace(
    WINDOW_MODAL=1, create_window=lambda **kwargs: Window(action["value"], **kwargs)))
sys.modules["orca"] = orca
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import opencreator_send_any as send

send.OpenCreatorSendPlugin().register_capabilities()
assert registered == [send.OpenCreatorSend]
capability = registered[0]()
assert capability.get_name() == "OpenCreator Send"

handle, path = tempfile.mkstemp(suffix=".gcode")
os.write(handle, b"M109 S200\nT0\n; filament used [mm] = 5\n")
os.close(handle)


def context(step="post", host="Moonraker", model="Flashforge Creator 5"):
    values = {"printer_model": model, "print_host": "192.0.2.1", "printhost_port": "", "printhost_apikey": ""}
    return types.SimpleNamespace(step=step, host=host, gcode_path=path, output_name="x.gcode",
                                 config_value=values.get, cancelled=lambda: False)


notes = []
orca.host.ui.message = lambda text, title: notes.append(text)
bundle = context(host="File")
bundle.output_name = os.path.join(os.path.dirname(path), ".1.0.3mf")
assert capability.execute(bundle)[0] == "skipped" and len(notes) == 1  # a send with Use 3MF on
exported = context(host="File")
exported.output_name = os.path.join(os.path.dirname(path), "sub", "part.gcode.3mf")
assert capability.execute(exported)[0] == "skipped" and len(notes) == 1  # a real export says nothing
assert capability.execute(context(step="other"))[0] == "skipped"
assert capability.execute(context(host="File"))[0] == "skipped"
assert capability.execute(context(model="Snapmaker U1"))[0] == "skipped"
assert capability.execute(context())[0] == "failure"  # Cancel send
action["value"] = "skip"
assert capability.execute(context())[0] == "success"  # Send unchanged
orca.host.ui.create_window = lambda **kwargs: 1 / 0
assert capability.execute(context()) [0] == "success"  # no window: an ordinary send carries on
os.remove(path)
print("entry point passed")
