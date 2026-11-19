# OpenCreator Send

An OrcaSlicer plugin for the Creator 5 and Creator 5 Pro on OpenCreator CFW. It opens a setup window when you send a sliced plate to the printer, then lets Orca's normal upload carry on.

It needs OrcaSlicer 2.5 or newer, with plugins.

## Install

On Linux or macOS, run the installer. It finds Orca's data folder, copies the plugin, and updates it when you run it again (the old file is kept as `.bak`; your saved options and log are left alone).

**Remote install:**

```sh
curl -fsSL https://raw.githubusercontent.com/wuyscc/opencreator-send/main/install.sh | bash
```

**Local install:**

```sh
git clone https://github.com/wuyscc/opencreator-send.git
cd opencreator-send
./install.sh
```

Options: `--dir PATH` (Orca's data folder, or `ORCA_DIR`), `--ref TAG` (version to download), `--check` (show what would change), `--uninstall` (add `--purge` to remove saved options and log too). Flatpak installs are found automatically.

Then:

1. Restart Orca and enable the plugin under File > Plugins.
   To install by hand instead, copy `opencreator_send_any.py` to `orca_plugins/opencreator_send/` in Orca's data folder (Help > Show Configuration Folder). The `_any` ending is the target OS suffix Orca Cloud asks for: the plugin is plain Python and runs on every OS.
2. In your Creator 5 print preset, add **OpenCreator Send** to the slicing pipeline plugin setting (`slicing_pipeline_plugin`), under Others.
3. Set the printer's connection to your Moonraker address. Orca asks for network and thread permission on first use.
4. Turn off **Use 3MF instead of G-code** under Printer settings > Basic information, and keep **Use printer agents instead of print hosts** off in Preferences. The window only opens on Orca's plain G-code upload; with Use 3MF on, the plugin says so and the print is sent unchanged.

The window remembers the options from your last send (`opencreator_send_prefs.json` next to the plugin) and opens with filaments already matched to lanes.

To see what it did on a send, read `opencreator_send.log` next to the plugin.

## What the window does

| Section | What it changes | How long it lasts |
| --- | --- | --- |
| **Filament to lane** | Which AFC lane feeds each tool the print uses, with `SET_MAP`. Shows each lane's filament, warns on another material and blocks on an empty lane. **Auto match** picks by material, then colour | Stays on the printer, like the printer screen's mapping |
| **This print only** | Flow calibration, purge, purge line, purge line for every tool, bed leveling, host pressure advance (when the printer has it) and the build plate check, as [`C5_PRINT_START` parameters](https://opencreator.readthedocs.io/en/latest/toggle-features/#for-one-print-only). The boxes start at the printer's switches | This print. The switches aren't touched |
| **Timelapse** | Adds `TIMELAPSE_TAKE_FRAME` at each layer change in this file, or removes it | This print. If the printer's timelapse is off or not in layer mode, ticking it turns that on, and that stays |

**Apply & send** changes the printer first and the file last, so a failure leaves the file as Orca made it. It's refused while the printer is printing, paused or running a macro, or when AFC has an error. **Send unchanged** skips setup, and works during a print. Queueing is Moonraker's own: with `queue_gcode_uploads: True` under `[file_manager]`, an **Upload and Print** that can't start joins its job queue, and the window tells you when that will happen. With `automatic_transition` off under `[job_queue]`, start the next job from Fluidd or Mainsail. **Cancel send** stops the send.

File exports and other printers' profiles don't open the window. If the window can't open, the send carries on unchanged.

## How options reach the printer

When an option differs from its switch, the plugin puts a `C5_PRINT_START` line at the top of the file, because an explicit line replaces the one the firmware generates. `start_command` builds it the same way, then adds the options. If `creator5_start_command` in Kalico's `virtual_sdcard.py` changes, change this too; the test below compares them. With no options changed, the file keeps using the firmware's own command.

## Tests

```sh
python3 tests/test_send.py path/to/kalico/klippy/extras/virtual_sdcard.py
python3 tests/test_entry.py
```

## Credits

Based on [FlashForge-C5-Modding-Group/opencreator-send](https://github.com/FlashForge-C5-Modding-Group/opencreator-send).
