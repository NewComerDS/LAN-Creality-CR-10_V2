#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Required Notice: Copyright (c) 2026 NewComerDS (https://github.com/NewComerDS/LAN-Creality-CR-10_V2)
"""
cp81.py v2 - bridge: Creality Print 7.1 (old-printer LAN protocol, port 81)
             <-> CR-10 V2 (Marlin) over USB serial.

Phase 1 (default): READ-ONLY. Real temperatures/position are reported to CP,
control commands from CP are only logged, nothing is sent to the printer
except M105/M114 queries.
Run with --live to actually execute commands (with safety limits below).
--print-enable (needs --live) turns print=<file> from a dry run into a real print streamed from this host.
--home-on-start additionally homes all axes once after connecting.

Install:  sudo apt install python3-serial      (or: pip install pyserial)
Run:      python3 cp81.py --serial /dev/ttyUSB0
          python3 cp81.py --serial /dev/ttyUSB0 --live
"""
__version__ = "3.1.0"

# Changelog (earlier entries reconstructed from the chat history)
#  3.1.0  BREAKING: the default allow-list is only 127.0.0.1 and the default FTP folder is ./ftp_root next to
#         the script -> existing installations must start with --allow 127.0.0.1,<PC IP>
#         settings that were hard-coded for one installation are now options (defaults unchanged):
#         --allow IP[,IP...] (clients that may talk to the bridge), --ftp-root DIR, --port N
#  3.0.4  G17 (select XY plane, written before G2/G3 arcs by the slicer) is allowed; during a print every line the
#         printer answers with 'Unknown command' is logged as a warning (e.g. arcs if the firmware has no ARC_SUPPORT)
#  3.0.3  FIX: the worker thread died on 'termios.error: (5, Input/output error)' (pyserial raises it unwrapped when
#         the USB adapter drops out, e.g. right after the printer is switched on) - the HTTP part kept answering,
#         nothing talked to the printer any more. Now: termios.error is a serial error, any other unexpected
#         exception is logged with a traceback and treated like a lost port, and a watchdog restarts the whole
#         service (systemd) if the worker thread is ever gone.
#  3.0.2  NO M114 while printing: Marlin's M114 waits until the planner is empty (planner.synchronize), so polling it
#         every 3 s made the printer slow down / stall; position is now estimated from the G-code that was sent
#         (exact M114 only after G28 and when pausing / stopping). Host-gap statistics in the log.
#         CP 'Invalid date': estimated time starts from the first M73 R value of the file and is never 0 while printing
#  3.0.1  stop: M410 no longer sent by default (STOP_QUICKSTOP) - after a stop during G28 the printer stopped
#         answering; lift for the stop is computed from a fresh M114 and only if Z is homed;
#         commands that arrive while the port is closed are dropped, the queue is flushed on every (re)connect
#  3.0.0  HOST PRINTING (off unless started with --print-enable): print=<file> streams the G-code line by line
#         (stop-and-wait for "ok"), progress / time left / state for CP, pause / resume / stop,
#         pre-flight (temperature limits, allowed codes, G28, coordinates inside the bed, file settled),
#         fault handling (printer error / over-temperature / reboot banner / silence / lost port -> heaters off,
#         port closed = board reset if even that fails), M73 handled locally
#  2.7.0  print=<file> -> DRY RUN only: maps CP's /media/... path to the FTP root and pre-checks the G-code
#         (nothing is sent to the printer); UTF-8 file names in the query (http.server decodes them as
#         latin-1); "M107 S50" from CP's fan slider is normalised to "M107"
#  2.6.0  whitelist: M106 [S0-255] / M107 (model fan) for gcodeCmd; fan state follows S value
#  2.5.0  CP bug workaround: an axis reported as exactly 0 is sent to CP as 0.01 (see CP_ZERO_EPS);
#         otherwise CP's Ie() locks the whole control panel after "Home" is clicked
#  2.4.0  position taken from Marlin's step counters (M114 'Count', logical Z was always 0.00);
#         --verbose; --version; CP "Home Z" while X/Y not homed -> full G28
#  2.3.0  waits up to 60 s for the board to boot, 3 timeouts before reopening the port,
#         MODEL = "CR-10" (CP has a preset for it; "CR-10 V2" broke the axis buttons)
#  2.2.0  optimistic position after accepted moves, per-move log, no echo:busy noise
#  2.1.0  refused-homing detection ("G28 Z Forbidden"), duplicate filter, --home-on-start, UNHANDLED log
#  2.0.0  first live bridge: M105/M114 polling, whitelist of G-code, safety limits

import argparse
import json
import os
import posixpath
import queue
import re
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import serial

try:                                                   # pyserial lets termios.error escape on a vanished USB adapter
    import termios
    SERIAL_ERRORS = (serial.SerialException, OSError, termios.error)
except ImportError:                                    # not Linux
    SERIAL_ERRORS = (serial.SerialException, OSError)

# ----------------------------------------------------------------- settings
SERIAL_PORT = "/dev/ttyUSB0"
BAUD = 115200                      # some boards use 250000
HTTP_PORT = 81
ALLOWED_IPS = {"127.0.0.1"}        # add the PC that runs Creality Print with --allow 127.0.0.1,<its IP>

MAC = "001122334455"               # must match ssid "CR10-<MAC>" already added in CP
MODEL = "CR-10"                    # CP needs a model name it has a preset for ("CR-10 V2" breaks the axis buttons)

FTP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ftp_root")   # = --root of cp81_ftp.py; CP's FTP login starts in what the printer calls /media
PRINTER_MEDIA = "/media"

MAX_NOZZLE = 260                   # degC, bridge-side hard limit
MAX_BED = 90                       # degC
LIMITS = {"X": (0, 300), "Y": (0, 300), "Z": (0, 400)}   # mm
MAX_FEED = 6000                    # mm/min
POLL_INTERVAL = 2.0                # s

LIVE = False                       # set by --live
# Creality Print's Ie("x"|"y"|"z", 0, ..) greys out the whole move panel for good when the axis
# value it reads at the moment of the "Home" click is already exactly 0 (its 150 ms timer sets the
# lock AFTER its 100 ms poll has released it). Never report an exact 0 to CP.
CP_ZERO_EPS = 0.01
VERBOSE = False                    # set by --verbose
PRINT_ENABLE = False               # set by --print-enable; otherwise print=<file> stays a dry run
STOP_QUICKSTOP = False            # send M410 first on stop? (not proven on the Creality firmware, see changelog 3.0.1)
BOUNDS_MARGIN = 2.0                # mm tolerated outside LIMITS in absolute G0/G1 moves
PRINT_SETTLE_S = 5                 # an uploaded file must have been unchanged for this long
PRINT_POLL_INTERVAL = 3.0          # s, M105 (temperatures only) between G-code lines
SILENCE_TIMEOUT = 60               # s without any byte from the printer = fault
PAUSE_LIFT = 10                    # mm the nozzle is raised on pause
PAUSE_RETRACT = 3                  # mm filament retract on pause
PAUSE_HEATER_TIMEOUT = 900         # s, the hotend is switched off after this long in pause
FINAL_STATE_HOLD = 120             # s, CP keeps seeing done / failed / stopped, then idle

# ------------------------------------------------------------------ helpers
NUM = r"-?\d+(?:\.\d+)?"
RE_FAN = re.compile(r"^(?:M106(?:\s+S(\d{1,3}))?|M107)$")
RE_G28 = re.compile(r"^G28(?:\s+[XYZ]0?)*$")
RE_MOVE = re.compile(rf"^G[01](?:\s+(?:[XYZ]{NUM}|F\d+))+$")
RE_TEMP_T = re.compile(rf"\bT:({NUM})\s*/({NUM})")
RE_TEMP_B = re.compile(rf"\bB:({NUM})\s*/({NUM})")
RE_COUNT = re.compile(r"Count\s+X:(-?\d+)\s+Y:(-?\d+)\s+Z:(-?\d+)")
STEPS_PER_MM = (80.0, 80.0, 400.0)  # CR-10 V2; checked against G28 reports: 8240/80=103, 12000/80=150, 5484/400=13.71
RE_POS = re.compile(rf"X:({NUM})\s+Y:({NUM})\s+Z:({NUM})")


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def check_gcode(line, homed):
    """Whitelist + limits. Returns (ok, reason)."""
    line = line.strip().upper()
    if line == "G90":
        return True, ""
    if RE_G28.match(line):
        return True, ""
    mf = RE_FAN.match(line)
    if mf:
        if mf.group(1) and int(mf.group(1)) > 255:
            return False, "fan S > 255"
        return True, ""
    if RE_MOVE.match(line):
        for tok in line.split()[1:]:
            ax, val = tok[0], float(tok[1:])
            if ax == "F":
                if val > MAX_FEED:
                    return False, f"feedrate {val} > {MAX_FEED}"
                continue
            lo, hi = LIMITS[ax]
            if not lo <= val <= hi:
                return False, f"{ax}{val} out of range {lo}..{hi}"
            if ax not in homed:
                return False, f"axis {ax} not homed"
        return True, ""
    return False, "command not in whitelist"


# ------------------------------------------------------------------ print files
# G-code that changes printer settings / power state - never allowed in a host-streamed print
FORBIDDEN_CODES = {"M112", "M500", "M501", "M502", "M503", "M92", "M206", "M301", "M303", "M304",
                   "M851", "M999", "M997", "M80", "M81", "M290", "M43", "M524"}


def first_m73_r(path):
    """minutes from the first 'M73 ... R<min>' of a G-code file (slicer's estimate), or None"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                if raw.startswith("M73") or raw.lstrip().upper().startswith("M73"):
                    m = re.search(r"\bR(\d+)", raw.upper())
                    if m:
                        return int(m.group(1))
    except OSError:
        pass
    return None


def resolve_print_path(printer_path):
    """CP: /media/mmcblk0p1/creality/gztemp/<file>  ->  local file below FTP_ROOT (or None)."""
    if not printer_path.startswith(PRINTER_MEDIA + "/"):
        return None
    rel = posixpath.normpath(printer_path[len(PRINTER_MEDIA):]).lstrip("/")
    root = os.path.realpath(FTP_ROOT)
    local = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, local]) != root:
        return None
    if not local.lower().endswith((".gcode", ".gco")) or not os.path.isfile(local):
        return None
    return local


# Codes a print may contain; anything else blocks the print (see preflight)
PRINT_ALLOWED_CODES = {"G17", "G0", "G1", "G2", "G3", "G4", "G10", "G11", "G21", "G28", "G29", "G90", "G91", "G92",
                       "M17", "M18", "M73", "M82", "M83", "M84", "M104", "M105", "M106", "M107", "M109", "M114",
                       "M117", "M118", "M140", "M190", "M201", "M203", "M204", "M205", "M220", "M221", "M280",
                       "M300", "M400", "M420", "M900", "T0"}


def norm_code(line):
    """'G01 X1' -> 'G1', 'm104 s200' -> 'M104'"""
    m = re.match(r"([GMT])0*(\d+)", line.strip().upper())
    return m.group(1) + m.group(2) if m else line.split()[0].upper()


def analyze_gcode(path):
    """Cheap pre-flight scan of a G-code file. Returns a dict (nothing is executed)."""
    codes, lines, nozzle, bed, forbidden = {}, 0, 0.0, 0.0, set()
    rel, shifted = False, False                     # G91 seen / G92 with X Y Z seen: coordinates not comparable
    lo, hi = {}, {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.split(";", 1)[0].strip().upper()
            if not line:
                continue
            lines += 1
            code = norm_code(line)
            codes[code] = codes.get(code, 0) + 1
            if code == "G90":
                rel = False
            elif code == "G91":
                rel = True
            elif code == "G92" and re.search(r"\b[XYZ]", line):
                shifted = True
            elif code in ("G0", "G1") and not rel and not shifted:
                for ax, val in re.findall(rf"\b([XYZ])({NUM})", line):
                    v = float(val)
                    lo[ax] = min(lo.get(ax, v), v)
                    hi[ax] = max(hi.get(ax, v), v)
            if code in FORBIDDEN_CODES:
                forbidden.add(code)
            ms = re.search(r"\bS(\d+(?:\.\d+)?)", line)
            if ms and code in ("M104", "M109"):
                nozzle = max(nozzle, float(ms.group(1)))
            if ms and code in ("M140", "M190"):
                bed = max(bed, float(ms.group(1)))
    out_of_bed = [f"{ax} {lo[ax]:g}..{hi[ax]:g}" for ax in lo
                  if lo[ax] < LIMITS[ax][0] - BOUNDS_MARGIN or hi[ax] > LIMITS[ax][1] + BOUNDS_MARGIN]
    return {"lines": lines, "bytes": os.path.getsize(path), "max_nozzle": nozzle, "max_bed": bed,
            "bounds": {ax: (lo[ax], hi[ax]) for ax in lo}, "out_of_bed": out_of_bed,
            "forbidden": sorted(forbidden), "has_G28": "G28" in codes,
            "unknown": sorted(c for c in codes if c not in PRINT_ALLOWED_CODES and c not in FORBIDDEN_CODES),
            "top_codes": sorted(codes.items(), key=lambda kv: -kv[1])[:8],
            "temps_ok": nozzle <= MAX_NOZZLE and bed <= MAX_BED}


def preflight(path, homed):
    """Returns (analysis, problems). A print is only started when 'problems' is empty."""
    a = analyze_gcode(path)
    problems = []
    if a["out_of_bed"]:
        problems.append(f"moves outside the printer volume (wrong printer profile?): {a['out_of_bed']}")
    if not a["temps_ok"]:
        problems.append(f"temperatures too high (nozzle {a['max_nozzle']:.0f} / bed {a['max_bed']:.0f}, "
                        f"limits {MAX_NOZZLE}/{MAX_BED})")
    if a["forbidden"]:
        problems.append(f"forbidden codes {a['forbidden']}")
    if a["unknown"]:
        problems.append(f"codes not on the allowed list {a['unknown']}")
    if not a["has_G28"] and not {"X", "Y", "Z"} <= homed:
        problems.append("no G28 in the file and the axes are not homed")
    if a["lines"] == 0:
        problems.append("file contains no commands")
    age = time.time() - os.path.getmtime(path)
    if age < PRINT_SETTLE_S:
        problems.append(f"file changed {age:.0f} s ago (upload still running?)")
    return a, problems


# ------------------------------------------------------------------ printer
class PrinterFault(Exception):
    """the printer reported an error / is in a state in which a print must not go on"""


class Printer:
    def __init__(self):
        self.ser = None
        self.job = None
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.connected = False
        self.homed = set()
        self.s = {
            "nozzle": 0.0, "nozzle_t": 0.0,
            "bed": 0.0, "bed_t": 0.0,
            "pos": (0.0, 0.0, 0.0),
            "fan": 0, "feed": 100,
        }

    def snapshot(self):
        with self.lock:
            return dict(self.s), set(self.homed), self.connected

    # --- serial
    def open(self):
        self.ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
        log("serial port opened, waiting for the board to boot ...")
        t0, ready = time.time(), False
        while time.time() - t0 < 60 and not ready:     # opening the port resets the board
            self.ser.write(b"M105\n")
            t1 = time.time()
            while time.time() - t1 < 3:
                if self.ser.readline().decode(errors="ignore").strip().startswith("ok"):
                    ready = True
                    break
        if not ready:
            self.ser.close()
            raise TimeoutError("printer did not answer M105 within 60 s after opening the port")
        time.sleep(1.0)                                # let duplicate answers to the extra M105s arrive
        self.ser.reset_input_buffer()
        self.flush_queue("new connection")                 # never run commands that were queued before a reset
        with self.lock:
            self.homed.clear()
            self.connected = True
        log("serial open:", SERIAL_PORT, BAUD, "- printer answers")
        global HOME_ON_START
        if HOME_ON_START and LIVE:
            HOME_ON_START = False                 # only once per script start
            log("home-on-start: queueing G28")
            self.q.put("G28")

    def flush_queue(self, why):
        dropped = []
        while True:
            try:
                dropped.append(self.q.get_nowait())
            except queue.Empty:
                break
        if dropped:
            log(f"{why}: dropped {len(dropped)} queued command(s): {dropped}")

    def close(self):
        with self.lock:
            self.connected = False
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass

    def send(self, cmd, timeout=20):
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\n").encode())
        out, t0 = [], time.time()
        while time.time() - t0 < timeout:
            line = self.ser.readline().decode(errors="ignore").strip()
            if not line:
                continue
            out.append(line)
            if line.lower().startswith("error"):
                log("PRINTER ERROR:", line)
            if line.startswith("ok"):
                return out
        raise TimeoutError(f"no 'ok' for {cmd}")

    # --- reading state
    def poll(self):
        for line in self.send("M105", 5):
            mt, mb = RE_TEMP_T.search(line), RE_TEMP_B.search(line)
            with self.lock:
                if mt:
                    self.s["nozzle"], self.s["nozzle_t"] = float(mt.group(1)), float(mt.group(2))
                if mb:
                    self.s["bed"], self.s["bed_t"] = float(mb.group(1)), float(mb.group(2))
        reply = self.send("M114", 5)
        if VERBOSE and reply != getattr(self, "_last_m114", None):
            self._last_m114 = reply
            log("M114 raw:", reply)
        for line in reply:
            mc, m = RE_COUNT.search(line), RE_POS.search(line)
            if mc or m:
                if mc:      # the firmware's logical Z stays 0.00 after homing, step counters are right
                    newpos = tuple(round(int(v) / k, 2) for v, k in zip(mc.groups(), STEPS_PER_MM))
                else:
                    newpos = tuple(float(x) for x in m.groups())
                with self.lock:
                    changed = newpos != self.s["pos"]
                    self.s["pos"] = newpos
                if changed:
                    log("pos: X%.2f Y%.2f Z%.2f" % newpos)
                break

    # --- helpers used while printing
    def send_ex(self, cmd, abs_timeout=60, silence=None):
        """Send one line and wait for its 'ok'. Every received line is examined (temperatures, errors).
        abs_timeout: total seconds; silence: max seconds without any byte from the printer."""
        silence = SILENCE_TIMEOUT if silence is None else silence
        self.ser.write((cmd + "\n").encode())
        out, t0 = [], time.time()
        last = t0
        while True:
            now = time.time()
            if now - t0 > abs_timeout:
                raise TimeoutError(f"no 'ok' for '{cmd}' within {abs_timeout:.0f} s")
            if now - last > silence:
                raise TimeoutError(f"printer silent for {silence:.0f} s after '{cmd}'")
            raw = self.ser.readline()
            if not raw:
                continue
            last = time.time()
            line = raw.decode(errors="ignore").strip()
            if not line:
                continue
            out.append(line)
            self._absorb(line)
            if line.startswith("ok"):
                return out

    def _absorb(self, line):
        """temperatures from any line; during a print also fatal conditions"""
        mt, mb = RE_TEMP_T.search(line), RE_TEMP_B.search(line)
        with self.lock:
            if mt:
                self.s["nozzle"], self.s["nozzle_t"] = float(mt.group(1)), float(mt.group(2))
            if mb:
                self.s["bed"], self.s["bed_t"] = float(mb.group(1)), float(mb.group(2))
            nozzle, bed = self.s["nozzle"], self.s["bed"]
        job = self.job
        if job is not None and job.active and "unknown command" in line.lower() and job.unknown_warned < 20:
            job.unknown_warned += 1                    # the printer ignored a line of the file - tell the user
            log(f"PRINTER WARNING (line {job.lines_sent}): {line}")
        if job is not None and job.active and not job.winding_down:
            if re.match(r"(error:|!!)", line, re.I):
                raise PrinterFault(line)
            if line.lower() == "start":
                raise PrinterFault("printer rebooted (start banner)")
            if nozzle > MAX_NOZZLE + 15 or bed > MAX_BED + 10:
                raise PrinterFault(f"over-temperature: nozzle {nozzle:.0f} / bed {bed:.0f}")

    def _parse_pos(self, lines):
        """position from an M114 reply -> (x, y, z, e) or None; also stored for CP"""
        for line in lines:
            mc, m = RE_COUNT.search(line), RE_POS.search(line)
            if mc or m:
                if mc:
                    pos = tuple(round(int(v) / k, 2) for v, k in zip(mc.groups(), STEPS_PER_MM))
                else:
                    pos = tuple(float(x) for x in m.groups())
                me = re.search(rf"\bE:({NUM})", line)
                with self.lock:
                    self.s["pos"] = pos
                return pos + (float(me.group(1)) if me else 0.0,)
        return None

    def _track(self, cmd):
        """fan / feed state shown to CP"""
        with self.lock:
            if cmd.startswith("M106"):
                ms = re.search(r"S(\d+)", cmd)
                self.s["fan"] = 1 if (int(ms.group(1)) > 0 if ms else True) else 0
            elif cmd.startswith("M107"):
                self.s["fan"] = 0
            elif cmd.startswith("M220"):
                ms = re.search(r"S(\d+)", cmd)
                if ms:
                    self.s["feed"] = int(ms.group(1))

    def start_job(self, local, printer_path):
        with self.lock:
            if self.job is not None and self.job.active:
                return False, "a print is already running"
            if not self.connected:
                return False, "printer not connected"
        self.job = PrintJob(self, local, printer_path)
        return True, ""

    def job_active(self):
        j = self.job
        return bool(j and j.active)

    def job_status(self):
        j = self.job
        if j is None:
            return None
        if not j.active and time.time() - j.finished_at > FINAL_STATE_HOLD:
            return None
        return j.status()

    def job_request(self, what):
        j = self.job
        return bool(j and j.active and j.request(what))

    # --- executing commands
    def run_cmd(self, cmd):
        log("->", cmd)
        reply = self.send(cmd, 240 if cmd.startswith("G28") else 30)
        for l in reply:
            if l != "ok" and not l.startswith("echo:busy"):
                log("   <-", l)
        # Marlin answers "ok" even when it refuses a homing move (e.g. "Home XY First")
        refused = any(re.search(r"home\s+\S+\s+first|forbidden|homing failed|halted", l, re.I) for l in reply)
        with self.lock:
            if cmd.startswith("G28"):
                if refused:
                    log("homing REFUSED by printer -> axes NOT marked as homed")
                else:
                    axes = re.findall(r"[XYZ]", cmd[3:]) or ["X", "Y", "Z"]
                    self.homed.update(axes)
            elif cmd.startswith("M106"):
                ms = re.search(r"S(\d+)", cmd)
                self.s["fan"] = 1 if (int(ms.group(1)) > 0 if ms else True) else 0
            elif cmd.startswith("M107"):
                self.s["fan"] = 0
            elif cmd.startswith("M220"):
                self.s["feed"] = int(cmd.split("S")[1])

    def worker(self):
        fails = 0
        while True:
            if not self.connected:
                try:
                    self.open()
                    fails = 0
                except Exception as e:
                    log("serial open failed:", e)
                    time.sleep(5)
                    continue
            try:
                job = self.job
                if job is not None and job.active:
                    job.step()                      # sends one G-code line / handles one request
                    fails = 0
                    continue
                try:
                    cmd = self.q.get(timeout=POLL_INTERVAL)
                except queue.Empty:
                    cmd = None
                if cmd:
                    self.run_cmd(cmd)
                self.poll()
                fails = 0
            except TimeoutError as e:
                fails += 1
                log(f"timeout ({fails}/3):", e)
                if fails >= 3:                      # only then reopen (that resets the board)
                    self.close()
                    fails = 0
                    time.sleep(3)
            except SERIAL_ERRORS as e:
                log(f"serial problem: {type(e).__name__}: {e}")
                if self.job is not None and self.job.active:
                    self.job.abort(f"serial connection lost: {e}")
                self.close()
                time.sleep(3)
            except Exception as e:                  # a bug must never leave the bridge half dead
                log("UNEXPECTED ERROR in worker (treated like a lost port):\n" + traceback.format_exc())
                if self.job is not None and self.job.active:
                    self.job.abort(f"internal error: {e!r}")
                self.close()                        # closing resets the board -> heaters off
                time.sleep(3)

    def predict(self, lines):
        """Store target coordinates of accepted G0/G1 lines right away, so the next
        Info answer already shows them (CP computes new targets from that value)."""
        with self.lock:
            pos = list(self.s["pos"])
            for l in lines:
                if re.match(r"G[01]\s", l):
                    for tok in l.split()[1:]:
                        if tok[0] in "XYZ":
                            pos["XYZ".index(tok[0])] = float(tok[1:])
            self.s["pos"] = tuple(pos)

    # --- translating CP parameters into G-code
    def plan(self, p):
        """p: dict of CP query params. Returns list of G-code lines (validated)."""
        lines = []
        st, homed, _ = self.snapshot()

        if "nozzleTemp2" in p:
            v = float(p["nozzleTemp2"])
            if 0 <= v <= MAX_NOZZLE:
                lines.append(f"M104 S{int(v)}")
            else:
                log(f"REJECT nozzleTemp2={v} (max {MAX_NOZZLE})")
        if "bedTemp2" in p:
            v = float(p["bedTemp2"])
            if 0 <= v <= MAX_BED:
                lines.append(f"M140 S{int(v)}")
            else:
                log(f"REJECT bedTemp2={v} (max {MAX_BED})")
        if "setFeedratePct" in p:
            v = int(float(p["setFeedratePct"]))
            if 10 <= v <= 200:
                lines.append(f"M220 S{v}")
            else:
                log(f"REJECT setFeedratePct={v}")
        if "fan" in p:
            lines.append("M106 S255" if p["fan"] == "1" else "M107")
        if "setPosition" in p:
            m = re.fullmatch(rf"([XYZ])({NUM})", p["setPosition"].strip().upper())
            if m:
                ax, val = m.groups()
                g = [f"G90", f"G1 {ax}{val} F{600 if ax == 'Z' else 3000}"]
                ok, why = check_gcode(g[1], homed)
                if ok:
                    cur = st["pos"]["XYZ".index(ax)]
                    log(f"move {ax}: {cur:.2f} -> {float(val):.2f}  ({float(val) - cur:+.2f})")
                    lines += g
                else:
                    log("REJECT setPosition:", why)
        if "gcodeCmd" in p:
            raw = [l.strip().upper() for l in p["gcodeCmd"].replace("\r", "").split("\n") if l.strip()]
            cur_homed = set(homed)
            good = []
            for l in raw:
                if re.match(r"M107\b", l):
                    l = "M107"                      # CP's fan slider sends e.g. "M107 S50"
                if RE_G28.match(l) and "Z" in l[3:] and not {"X", "Y"} <= cur_homed:
                    log(f"'{l}' refused by Marlin while X/Y are not homed -> sending full G28")
                    l = "G28"
                ok, why = check_gcode(l, cur_homed)
                if not ok:
                    log(f"REJECT gcodeCmd '{l}': {why}")
                    good = []
                    break
                if l.startswith("G28"):       # allow G28 followed by moves in one request
                    cur_homed.update(re.findall(r"[XYZ]", l[3:]) or ["X", "Y", "Z"])
                good.append(l)
            lines += good

        if "print" in p:
            local = resolve_print_path(p["print"])
            if not local:
                log(f"PRINT REJECTED (not under {PRINTER_MEDIA}/..., not a .gcode, or file missing): {p['print']}")
            else:
                an, problems = preflight(local, homed)
                log(f"PRINT pre-flight: {local}")
                log(f"   {an['lines']} lines, {an['bytes']} bytes | max nozzle {an['max_nozzle']:.0f} C, max bed {an['max_bed']:.0f} C"
                    f" (limits {MAX_NOZZLE}/{MAX_BED}) | has G28: {an['has_G28']} | bounds {an['bounds']}")
                log(f"   forbidden: {an['forbidden'] or 'none'} | not on allowed list: {an['unknown'] or 'none'} | "
                    f"most used: {an['top_codes']}")
                if not (PRINT_ENABLE and LIVE):
                    log("PRINT DRY RUN - nothing is sent to the printer (printing needs --live --print-enable)"
                        + (f"; it would be refused: {'; '.join(problems)}" if problems else ""))
                elif problems:
                    log("PRINT REFUSED: " + "; ".join(problems))
                else:
                    ok, why = self.start_job(local, p["print"])
                    if not ok:
                        log(f"PRINT REFUSED: {why}")

        if "pause" in p:
            what = "pause" if p["pause"] == "1" else "resume"
            log(f"print: {what} requested" if self.job_request(what) else f"print: {what} ignored (no print / wrong state)")
        if p.get("stop") == "1":
            log("print: stop requested" if self.job_request("stop") else "print: stop ignored (no print running)")

        # while a print is running only temperature / fan / speed changes are accepted
        if self.job_active():
            kept = [l for l in lines if re.match(r"(M104|M140|M106|M107|M220)\b", l)]
            if len(kept) != len(lines):
                log(f"print running: only temperature / fan / speed changes are accepted, dropped {[l for l in lines if l not in kept]}")
            lines = kept

        # not implemented
        for key in ("led",):
            if key in p:
                log(f"NOT IMPLEMENTED: {key}={p[key]}")
        return lines


class PrintJob:
    """Streams one G-code file to Marlin: next line only after the 'ok' of the previous one.
    Runs inside the printer worker thread (Printer.worker calls step() while a job is active)."""

    PRINTING, PAUSED, DONE, FAILED, STOPPED = 1, 5, 2, 3, 4     # CP "state" numbers (K1 convention, not verified)
    NAMES = {1: "printing", 5: "paused", 2: "done", 3: "failed", 4: "stopped"}

    def __init__(self, printer, local, printer_path):
        self.p, self.local, self.path = printer, local, printer_path
        self.size = max(os.path.getsize(local), 1)
        self.f = open(local, "rb")
        self.state, self.active, self.error = self.PRINTING, True, ""
        self.start = time.time()
        self.paused_total, self.paused_since = 0.0, None
        self.bytes_done = self.lines_sent = 0
        self.m73_p = self.m73_r = None            # progress % / minutes left, as announced by the slicer
        self.m73_r = first_m73_r(local)           # slicer's estimate, known before the first M73 line is sent
        self.e_mode = "absolute"                  # Marlin default (M82); M83 switches to relative
        self.req = None
        self.rel = False                          # G91 seen: sent coordinates are relative
        self.t_last_ok, self.gap_max, self.gap_big, self.gap_n = None, 0.0, 0, 0   # host-gap statistics
        self.unknown_warned = 0                   # 'Unknown command' warnings logged so far
        self.winding_down = False                 # True while stopping / failing: do not raise on error lines again
        self.saved, self.saved_nozzle_t, self.hotend_off = None, 0.0, False
        self.finished_at = None
        self.last_poll = self.last_log = time.time()
        log(f"PRINT STARTED: {local} ({self.size} bytes)")

    # ------------------------------------------------------------ status for CP / requests from CP
    def status(self):
        now = time.time()
        paused = (now - self.paused_since) if self.paused_since else 0.0
        elapsed = max(0.0, now - self.start - self.paused_total - paused)
        if self.state == self.DONE:
            prog, left = 100.0, 0.0
        else:
            prog = float(self.m73_p) if self.m73_p is not None else 100.0 * self.bytes_done / self.size
            if self.m73_r is not None:
                left = self.m73_r * 60.0
            else:
                left = elapsed * (100.0 - prog) / prog if prog >= 1 else 0.0
        if self.state in (self.PRINTING, self.PAUSED):
            left = max(left, 60.0)                 # CP shows "Invalid date" for an estimate of 0
        return {"state": self.state, "path": self.path, "progress": min(prog, 100.0), "elapsed": elapsed,
                "left": max(left, 0.0), "start": self.start, "error": self.error}

    def request(self, what):
        if self.req == "stop":
            return True
        if what == "pause" and self.state == self.PRINTING:
            self.req = "pause"
        elif what == "resume" and self.state == self.PAUSED:
            self.req = "resume"
        elif what == "stop" and self.state in (self.PRINTING, self.PAUSED):
            self.req = "stop"
        else:
            return False
        return True

    # ------------------------------------------------------------ life cycle
    def _end(self, state, error=""):
        self.state, self.error, self.active = state, error, False
        self.finished_at = time.time()
        try:
            self.f.close()
        except Exception:
            pass
        log(f"PRINT {self.NAMES[state].upper()}" + (f": {error}" if error else "") +
            f" | {self.lines_sent} lines sent, {self.status()['progress']:.1f} %" + self._gap_text())

    def abort(self, reason):
        """port is gone - nothing can be sent any more"""
        if self.active:
            self._end(self.FAILED, reason)

    def _heaters_off(self):
        """M104/M140/M107, every one tried; returns False if one of the heater commands failed"""
        ok = True
        for c in ("M104 S0", "M140 S0", "M107"):
            try:
                self.p.send_ex(c, 10, 10)
                self.p._track(c)
            except Exception as e:
                log(f"   could not send '{c}': {e}")
                if c != "M107":
                    ok = False
        if not ok:
            log("heaters could not be switched off -> closing the serial port (the board resets, heaters go off)")
            self.p.close()
        return ok

    def _fail(self, reason):
        """fatal error: heaters off, print is over"""
        log("PRINT FAULT:", reason)
        self.winding_down = True
        self._heaters_off()
        self._end(self.FAILED, reason)

    def step(self):
        try:
            self._step()
        except PrinterFault as e:
            self._fail(str(e))
        except TimeoutError as e:
            self._fail(f"timeout: {e}")
        # serial exceptions propagate to the worker, which calls abort()

    def _step(self):
        p = self.p
        self._drain_queue()
        if time.time() - self.last_poll >= PRINT_POLL_INTERVAL:
            self._poll()
        req, self.req = self.req, None
        if req == "stop":
            return self._do_stop()
        if req == "pause":
            return self._do_pause()
        if req == "resume":
            return self._do_resume()
        if self.state == self.PAUSED:
            if (not self.hotend_off and self.saved_nozzle_t > 0
                    and time.time() - self.paused_since > PAUSE_HEATER_TIMEOUT):
                log("pause time-out: switching the hotend off")
                p.send_ex("M104 S0", 30)
                p._track("M104 S0")
                self.hotend_off = True
            time.sleep(0.2)
            return
        line = self._next_line()
        if line is None:
            return self._do_finish()
        self._send_gcode(line)

    # ------------------------------------------------------------ small helpers
    def _drain_queue(self):
        """temperature / fan / speed changes requested from CP while printing"""
        while True:
            try:
                cmd = self.p.q.get_nowait()
            except queue.Empty:
                return
            if not re.match(r"(M104|M140|M106|M107|M220)\b", cmd):
                log(f"print running: '{cmd}' dropped")
                continue
            log("-> (during print)", cmd)
            self.p.send_ex(cmd, 120)
            self.p._track(cmd)

    def _poll(self):
        """temperatures only: Marlin's M114 waits for an empty planner and would stall the print"""
        self.last_poll = time.time()
        self.p.send_ex("M105", 60)
        if time.time() - self.last_log >= 60:
            self.last_log = time.time()
            st, s = self.status(), self.p.snapshot()[0]
            log(f"print {self.NAMES[self.state]}: {st['progress']:.1f} %, left {st['left'] / 60:.0f} min | "
                f"nozzle {s['nozzle']:.0f}/{s['nozzle_t']:.0f}, bed {s['bed']:.0f}/{s['bed_t']:.0f}"
                + self._gap_text())

    def _gap_text(self):
        if not self.gap_n:
            return ""
        return f" | host gaps: max {self.gap_max * 1000:.0f} ms, >100 ms: {self.gap_big} of {self.gap_n} lines"

    def _note_move(self, up):
        """keep the position shown in CP up to date without asking the printer"""
        if self.rel:
            return
        toks = re.findall(rf"\b([XYZ])({NUM})", up)
        if not toks:
            return
        with self.p.lock:
            pos = list(self.p.s["pos"])
            for ax, v in toks:
                pos["XYZ".index(ax)] = float(v)
            self.p.s["pos"] = tuple(pos)

    def _next_line(self):
        while True:
            raw = self.f.readline()
            if not raw:
                return None
            self.bytes_done += len(raw)
            line = raw.decode("utf-8", "replace").split(";", 1)[0].strip()
            if line:
                return line

    @staticmethod
    def _timeout_for(code):
        if code in ("M109", "M190"):
            return 2400                      # heating can take a long time
        if code in ("G28", "G29", "M400", "G4"):
            return 900
        return 120

    def _send_gcode(self, line):
        p = self.p
        code = norm_code(line)
        up = line.upper()
        if code == "M73":                    # progress info for the display - used here, not forwarded
            mp, mr = re.search(r"\bP(\d+(?:\.\d+)?)", up), re.search(r"\bR(\d+)", up)
            if mp:
                self.m73_p = float(mp.group(1))
            if mr:
                self.m73_r = int(mr.group(1))
            return
        if code in FORBIDDEN_CODES or code not in PRINT_ALLOWED_CODES:
            raise PrinterFault(f"G-code '{line}' is not allowed in a print")
        ms = re.search(r"\bS(\d+(?:\.\d+)?)", up)
        if code in ("M104", "M109") and ms and float(ms.group(1)) > MAX_NOZZLE:
            raise PrinterFault(f"'{line}' exceeds the nozzle limit {MAX_NOZZLE}")
        if code in ("M140", "M190") and ms and float(ms.group(1)) > MAX_BED:
            raise PrinterFault(f"'{line}' exceeds the bed limit {MAX_BED}")
        if code in ("M104", "M109", "M140", "M190", "G28", "G29", "M400"):
            log("print ->", line)
        now = time.time()
        if self.t_last_ok is not None and self.state == self.PRINTING:
            gap = now - self.t_last_ok                 # time the host needed between 'ok' and the next line (polls included)
            if gap < 30:
                self.gap_n += 1
                self.gap_max = max(self.gap_max, gap)
                if gap > 0.1:
                    self.gap_big += 1
        reply = p.send_ex(line, self._timeout_for(code))
        self.t_last_ok = time.time()
        self.lines_sent += 1
        if code == "G90":
            self.rel = False
        elif code == "G91":
            self.rel = True
        elif code in ("G0", "G1"):
            self._note_move(up)
        if code == "M82":
            self.e_mode = "absolute"
        elif code == "M83":
            self.e_mode = "relative"
        elif code == "G28":
            if any(re.search(r"home\s+\S+\s+first|forbidden|homing failed", l, re.I) for l in reply):
                raise PrinterFault("homing refused by the printer")
            with p.lock:
                p.homed.update(re.findall(r"[XYZ]", up[3:]) or ["X", "Y", "Z"])
            self._read_pos()                           # planner is empty after homing, M114 costs nothing here
        elif code in ("M106", "M107", "M220"):
            p._track(line)

    # ------------------------------------------------------------ end / pause / resume / stop
    def _do_finish(self):
        p = self.p
        log("print: end of file - waiting for the last moves")
        p.send_ex("M400", 1800)
        for c in ("M104 S0", "M140 S0", "M107"):
            p.send_ex(c, 60)
            p._track(c)
        self._end(self.DONE)

    def _read_pos(self):
        return self.p._parse_pos(self.p.send_ex("M114", 60))

    def _do_pause(self):
        p = self.p
        log("PAUSE: waiting for the moves in the buffer ...")
        p.send_ex("M400", 900)
        pos = self._read_pos()
        self.saved_nozzle_t = p.snapshot()[0]["nozzle_t"]
        self.hotend_off = False
        self.saved = pos
        if pos is None:
            log("pause: position unknown -> not parking, only stopping the feed")
        else:
            x, y, z, e = pos
            p.send_ex("M83", 30)                                    # relative extruder for the retract
            p.send_ex(f"G1 E-{PAUSE_RETRACT:g} F1800", 60)
            lift = min(PAUSE_LIFT, LIMITS["Z"][1] - z)
            if lift >= 1:
                p.send_ex("G91", 30)
                p.send_ex(f"G1 Z{lift:g} F600", 120)
                p.send_ex("G90", 30)
            log(f"pause: parked, saved X{x:.2f} Y{y:.2f} Z{z:.2f} E{e:.2f}")
        self.state, self.paused_since = self.PAUSED, time.time()

    def _do_resume(self):
        p = self.p
        log("RESUME")
        s = p.snapshot()[0]
        if self.saved_nozzle_t > 0 and (self.hotend_off or s["nozzle"] < self.saved_nozzle_t - 5):
            p.send_ex(f"M109 S{int(self.saved_nozzle_t)}", 2400)
            p._track("M104")
        if self.saved:
            x, y, z, e = self.saved
            p.send_ex("G90", 30)
            p.send_ex(f"G1 X{x:.2f} Y{y:.2f} F3000", 120)
            p.send_ex(f"G1 Z{z:.2f} F600", 120)
            p.send_ex("M83", 30)
            p.send_ex(f"G1 E{PAUSE_RETRACT:g} F1800", 60)           # undo the retract
            if self.e_mode == "absolute":
                p.send_ex("M82", 30)                                # E register is back at its old value
        self.paused_total += time.time() - (self.paused_since or time.time())
        self.paused_since, self.state = None, self.PRINTING

    def _do_stop(self):
        p = self.p
        log("STOP requested")
        self.winding_down = True
        if STOP_QUICKSTOP:
            try:
                p.send_ex("M410", 30)                               # quick stop (ignored if unsupported)
            except Exception as e:
                log(f"   M410 failed: {e}")
        if not self._heaters_off():
            self._end(self.STOPPED, "heaters could not be switched off, port closed")
            return
        try:
            with p.lock:
                z_homed = "Z" in p.homed
            pos = self._read_pos() if z_homed else None             # fresh position, the cached one can be old
            if pos:
                lift = min(PAUSE_LIFT, LIMITS["Z"][1] - pos[2])
                if lift >= 1:
                    p.send_ex("G91", 30)
                    p.send_ex(f"G1 Z{lift:g} F600", 120)
                    p.send_ex("G90", 30)
            else:
                log("stop: Z not homed or position unknown -> not lifting the nozzle")
            p.send_ex("M84 X Y E", 30)                              # Z stays energised
        except (PrinterFault, TimeoutError) as e:
            self._end(self.STOPPED, f"stopped, parking incomplete: {e}")
            return
        self._end(self.STOPPED)

printer = Printer()
LAST = {"k": None, "t": 0.0}      # CP sends every command twice
HOME_ON_START = False


# --------------------------------------------------------------------- HTTP
def build_info():
    s, homed, connected = printer.snapshot()
    x, y, z = s["pos"]
    info = {
        "error": 0,
        "ssid": f"CR10-{MAC}",
        "model": MODEL,
        "connect": 1 if connected else 0,
        "state": 0,                                   # 0 = idle (other values not verified)
        "nozzleTemp": round(s["nozzle"], 1),
        "nozzleTemp2": round(s["nozzle_t"], 1),       # target
        "bedTemp": round(s["bed"], 1),
        "bedTemp2": round(s["bed_t"], 1),             # target
        "printProgress": 0,
        "curFeedratePct": s["feed"],
        "fan": s["fan"],
        "autohome": 1 if {"X", "Y"} <= homed else 0,     # CP enables the move buttons on this; Z is still checked in the bridge
        "curPosition": "X:{:.2f} Y:{:.2f} Z:{:.2f}".format(*(v if v != 0 else CP_ZERO_EPS for v in (x, y, z))),
        "printLeftTime": 0,
        "printJobTime": 0,
        "modelVersion": "V2",
        "video": 0,                                   # 0 -> CP falls back to http://IP:8080/?action=stream
    }
    job = printer.job_status()
    if job:
        info.update({"state": job["state"], "print": job["path"], "printProgress": int(job["progress"]),
                     "printJobTime": int(job["elapsed"]), "printLeftTime": int(job["left"]),
                     "printStartTime": int(job["start"])})
    return info


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.client_address[0] not in ALLOWED_IPS:
            log("DENIED client", self.client_address[0])
            self.send_json({"error": 1}, 403)
            return

        try:                                           # http.server decodes the request line as latin-1,
            self.path = self.path.encode("latin-1").decode("utf-8")   # CP sends UTF-8 file names unescaped
        except UnicodeError:
            pass
        u = urlparse(self.path)
        q = parse_qs(u.query)
        f, o, fn = (q.get(k, [""])[0] for k in ("fname", "opt", "function"))

        if u.path == "/protocal.csp" and (f, o, fn) == ("Info", "main", "get"):
            info = build_info()
            if VERBOSE:
                log("Info poll ->", {k: info[k] for k in ("connect", "state", "autohome", "curPosition", "nozzleTemp", "bedTemp")})
            self.send_json(info)
            return

        if u.path == "/protocal.csp" and (f, o, fn) == ("net", "iot_conf", "set"):
            p = {k: v[0] for k, v in q.items() if k not in ("fname", "opt", "function")}
            key = json.dumps(p, sort_keys=True)
            if key == LAST["k"] and time.time() - LAST["t"] < 1.0:
                self.send_json({"error": 0})          # duplicate from CP, ignore
                return
            LAST["k"], LAST["t"] = key, time.time()
            if set(p) - {"ReqPrinterPara"}:           # ignore the plain heartbeat
                log("CP COMMAND:", p)
                try:
                    lines = printer.plan(p)
                except (ValueError, KeyError) as e:
                    log("bad parameters:", e)
                    lines = []
                for l in lines:
                    if not LIVE:
                        log("[read-only] would send:", l)
                    elif printer.connected:
                        printer.q.put(l)
                    else:
                        log("printer not connected - command dropped:", l)
                if LIVE and printer.connected:
                    printer.predict(lines)
            self.send_json({"error": 0})
            return

        log("UNHANDLED:", self.path)
        self.send_json({"error": 0})


def main():
    global LIVE, SERIAL_PORT, HOME_ON_START, VERBOSE, PRINT_ENABLE, ALLOWED_IPS, FTP_ROOT, HTTP_PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=SERIAL_PORT)
    ap.add_argument("--allow", default=",".join(sorted(ALLOWED_IPS)),
                    help="comma separated IPs that may talk to the bridge (the PC running Creality Print)")
    ap.add_argument("--ftp-root", default=FTP_ROOT,
                    help="folder served by cp81_ftp.py; print=<file> is looked up there")
    ap.add_argument("--port", type=int, default=HTTP_PORT, help="HTTP port of the fake printer (Creality Print expects 81)")
    ap.add_argument("--live", action="store_true", help="actually send commands to the printer")
    ap.add_argument("--version", action="version", version=f"cp81 {__version__}")
    ap.add_argument("--verbose", action="store_true", help="log every Info poll from CP and raw M114 replies")
    ap.add_argument("--print-enable", action="store_true",
                    help="print=<file> really prints (host streaming); needs --live. Without it: dry run")
    ap.add_argument("--home-on-start", action="store_true",
                    help="run one G28 (home all axes) after the first serial connect; needs --live")
    a = ap.parse_args()
    LIVE, SERIAL_PORT, HOME_ON_START, VERBOSE = a.live, a.serial, a.home_on_start, a.verbose
    PRINT_ENABLE = a.print_enable and a.live
    ALLOWED_IPS = {x.strip() for x in a.allow.split(",") if x.strip()}
    FTP_ROOT, HTTP_PORT = os.path.abspath(a.ftp_root), a.port

    wt = threading.Thread(target=printer.worker, daemon=True)
    wt.start()

    def watchdog():                                 # last line of defence: restart the service if the worker is gone
        while True:
            time.sleep(5)
            if not wt.is_alive():
                log("WATCHDOG: worker thread is dead -> exiting so that systemd restarts the service")
                os._exit(1)
    threading.Thread(target=watchdog, daemon=True).start()
    log(f"cp81 v{__version__} | HTTP on 0.0.0.0:{HTTP_PORT} | mode: {'LIVE' if LIVE else 'READ-ONLY'}"
        f" | host printing: {'ENABLED' if PRINT_ENABLE else 'off (dry run)'}")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
