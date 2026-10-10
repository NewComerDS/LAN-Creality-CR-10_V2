[Русский](README.md) | **English**

# cp81 - Connecting an “old” printer running Marlin to Creality Print 7.x via the network. (tested: CR-10 V2)

Creality Print 7.1 can talk to the old Creality WiFi-box printers over the LAN, but a bare CR-10 V2 does not
speak that protocol. **cp81 pretends to be such a printer** (a small Python service on a Raspberry Pi / Orange Pi
connected to the printer by USB) and translates what Creality Print sends into G-code for Marlin.

<img width="1591" height="849" alt="2026-10-08 122314" src="https://github.com/user-attachments/assets/e4daf53c-8293-4020-8692-eb3e6dcec2e7" />

```
Creality Print (PC) --HTTP :81--> cp81.py ----USB serial----> CR-10 V2 (Marlin)
                    --FTP  :21--> cp81_ftp.py (uploaded G-code in ./ftp_root, streamed by cp81.py)
                    (camera: optional MJPEG stream on :8080, started separately)
```

> **Unofficial.** Not affiliated with or endorsed by Creality. No Creality code is included; the protocol notes
> below were obtained by observing the network traffic and the behaviour of the program.

## What works

* printer shows up in Creality Print, live temperatures, target temperatures, position, fan on/off
* homing, moving the axes, nozzle / bed temperature, print speed, fan
* G-code upload and file list / delete from Creality Print (own small FTP server)
* **printing from the host**: the G-code file is streamed over serial line by line; progress and time left in
  Creality Print, pause / resume / stop, safety checks and fault handling (see below)

## Safety - read this first

Printing from the host means **this program is responsible for the heaters while it prints**.

* Never leave a print unattended, especially the first ones. The only real emergency stop is the printer's power switch;
  the *stop* button in Creality Print waits for the G-code line that is currently running (e.g. homing).
* If the Pi hangs or the USB cable drops during a print, the printer stops where it is and the heaters stay on
  (only Marlin's own thermal protection watches them). Restarting the service resets the board - that kills the print.
* Host-side limits: nozzle <= 260 C, bed <= 90 C, moves inside the bed, a list of allowed G-codes, forbidden codes
  (`M500`, `M112`, `M81`, ...). Edit the constants at the top of `cp81.py` for your printer.
* Pre-flight check before every print (temperatures, allowed codes, coordinates, `G28`). Without `--print-enable`
  the `print` command is only a dry run.
* Use it at your own risk (see the disclaimer in LICENSE).

## Requirements

* Creality Print 7.1 (Windows), a printer with Marlin and a USB serial port (115200 baud)
* a small Linux computer next to the printer, Python 3.9+, `pyserial`
* the PC and the Linux computer in the same network; the bridge listens on port 81, the FTP server on port 21 (root)

## Quick start

```bash
git clone https://github.com/NewComerDS/LAN-Creality-CR-10_V2.git
cd LAN-Creality-CR-10_V2
sudo apt install python3-serial           # or: pip install -r requirements.txt

# 1) read-only: temperatures and position only, nothing is sent to the printer except M105/M114
python3 cp81.py --serial /dev/ttyUSB0 --allow 127.0.0.1,<IP of the PC>
```
In Creality Print add a device **by IP address** (the address of the Linux computer). It should appear as a CR-10.

```bash
# 2) controls: homing, moving, temperatures (opening the serial port resets the printer board)
python3 cp81.py --serial /dev/ttyUSB0 --allow 127.0.0.1,<IP of the PC> --live

# 3) file upload: run the FTP server (needs root for port 21), upload a file from Creality Print
sudo python3 cp81_ftp.py --root ./ftp_root --allow 127.0.0.1,<IP of the PC> --verbose

# 4) printing: first the no-heat test file examples/air_test.gcode, with an EMPTY bed and you standing next to it
python3 cp81.py --serial /dev/ttyUSB0 --allow 127.0.0.1,<IP of the PC> --live --print-enable
```
Both programs must use the same folder (`--ftp-root` of `cp81.py` = `--root` of `cp81_ftp.py`; default: `./ftp_root`).
Autostart: `systemd/*.service` (they assume the files are in `/opt/cp81`; adapt paths and IPs, then `systemctl enable --now`).

### Options

| `cp81.py` | |
|---|---|
| `--serial DEV` | serial port of the printer (use `/dev/serial/by-id/...` for a stable name) |
| `--allow IPS` | comma separated client IPs, default only `127.0.0.1` |
| `--ftp-root DIR` | where uploaded G-code is looked up |
| `--live` | really send commands (without it everything is only logged) |
| `--print-enable` | `print=` really prints; needs `--live` |
| `--home-on-start` | one `G28` after the first connect (needs `--live`) |
| `--verbose` | log every poll from Creality Print |

`cp81_ftp.py`: `--root`, `--allow`, `--port`, `--max-upload-mb`, `--list-style`, `--verbose`.

## Protocol notes (how Creality Print is persuaded)

* **Discovery / status**: `GET :81/protocal.csp?fname=Info&opt=main&function=get`, about every 5 s, answered with JSON.
  `model` must be a name Creality Print has a preset for (`"CR-10"`; `"CR-10 V2"` broke the axis buttons) and `ssid` is
  `CR10-<12 hex digits>`.
* **Info fields used**: `nozzleTemp`/`bedTemp` (current), `nozzleTemp2`/`bedTemp2` (targets), `curPosition`
  (`"X:.. Y:.. Z:.."`), `autohome` (0/1), `curFeedratePct`, `fan` (0/1 - any non-zero is shown as 100 %),
  `printProgress`, `printLeftTime` and `printJobTime` (seconds), `print` (file path), `state`, `video`.
* **Commands**: `GET :81/protocal.csp?fname=net&opt=iot_conf&function=set&<params>` with `nozzleTemp2`, `bedTemp2`,
  `setFeedratePct`, `setPosition=<axis><absolute mm>`, `gcodeCmd` (also used by the fan slider: `M106 S..`),
  `print=/media/mmcblk0p1/creality/gztemp/<file>`, `pause=1|0`, `stop=1`. Every command arrives twice.
* **Camera**: with `"video": 0` Creality Print falls back to `http://<ip>:8080/?action=stream` (MJPEG).
* **Files**: FTP, user `anonymous`, relative `CWD mmcblk0p1/creality/gztemp`, `LIST` (the listing is only shown when it
  looks like vsftpd output), `STOR` for upload, `DELE` for delete.
* **Quirks worked around**: an axis value of exactly 0 in `curPosition` locks the move panel for good after "Home" is
  clicked (reported as 0.01); Marlin refuses `G28 Z` while X/Y are not homed (the bridge sends a full `G28`);
  Marlin's `M114` waits for an empty move buffer, so it is not used while printing; the firmware reports its logical Z
  as 0 after homing, so positions come from the step counters.

## Known limitations

* The fan is shown on/off only; the Z arrows are inverted for printers whose gantry (not the bed) moves in Z, because
  Creality Print assumes a moving bed - not fixable from the bridge.
* The state number for "paused" (5) is a guess; behaviour of other printers / firmwares is untested.
* No automatic recovery of a print after a restart of the service or the printer.

## Files

`cp81.py` bridge | `cp81_ftp.py` FTP server | `README.ru.md` Russian version | `tools/ftp_probe.py` throw-away FTP server that logs everything
(useful to see what Creality Print does) | `examples/air_test.gcode` no-heat test print | `systemd/` unit files

## Telegram Notifications

In version 3.2.0, the function for notifying about completion or printing issues has been added. 

How to enable
In Telegram, find @BotFather → /newbot → you will receive a token in the form of 123456:ABC....
Write any message to your new bot (for example, /start), then find out the chat_id:
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"

In the answer, find "chat":{"id":<number>....
3. Place the settings in a file outside the project, accessible only by root:

 in /etc/cp81.env

   CP81_TG_TOKEN=123456:ABC...
   CP81_TG_CHAT=123456789

   sudo chmod 600 /etc/cp81.env

4. In /etc/systemd/system/cp81.service, add the following line to the [Service] section:

   EnvironmentFile=/etc/cp81.env

then run sudo systemctl daemon-reload && sudo systemctl restart cp81 (not during printing).
5. Test the sending without launching the bridge:

   sudo bash -c 'set -a; . /etc/cp81.env; set +a; python3 /var/www/cp/cp81.py --tg-test'

It should display “Telegram test message sent,” and a message will appear in the chat. In the log, at startup, there will be the line “Telegram notifications: ON.”

## License

[PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)
(`PolyForm-Noncommercial-1.0.0`). In plain words: you may use, copy, modify and share this project **free of charge for
noncommercial purposes** (hobby, personal, research, education, charity). **Selling it, or using it commercially, is not
allowed** without the written permission of the author.

## newcomerds

[![Boosty](https://img.shields.io/badge/Boosty-%D0%9F%D0%BE%D0%B4%D0%B4%D0%B5%D1%80%D0%B6%D0%B0%D1%82%D1%8C-FF6B35?style=for-the-badge&logo=boosty)](https://boosty.to/newcomerds/donate)
