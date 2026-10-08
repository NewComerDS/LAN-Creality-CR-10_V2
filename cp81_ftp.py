#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Required Notice: Copyright (c) 2026 NewComerDS (https://github.com/NewComerDS/LAN-Creality-CR-10_V2)
"""
cp81_ftp.py - FTP side of the CR-10 V2 <-> Creality Print bridge (companion of cp81.py).

What Creality Print does (seen with ftp_probe.py): logs in as "anonymous", changes into
mmcblk0p1/creality/gztemp, lists it (LIST) and uploads G-code with STOR.  cp81.py later reads the
same files from --root, so both must use the same folder (FTP_ROOT in cp81.py).

Rules enforced here
  * only clients from the allow-list (default: 127.0.0.1 only, add the PC running Creality Print),
  * only the user "anonymous" / "ftp",
  * reading and listing anywhere below --root, nothing outside it,
  * writing / deleting / renaming ONLY inside WRITE_DIR, only .gcode / .gco names,
  * uploads go to a hidden ".name.part" file and appear under their real name only when the
    transfer has finished (a half-uploaded file is never visible to a print),
  * size limit and minimum free disk space, idle time-outs, connection limit.

Run (port 21 needs root):  sudo python3 cp81_ftp.py --root ./ftp_root --allow 127.0.0.1,<IP of the PC>
"""
__version__ = "1.2.0"

# Changelog
#  1.2.0  BREAKING: the default allow-list is only 127.0.0.1 and the default --root is ./ftp_root next to the
#         script -> existing installations must start with --allow 127.0.0.1,<PC IP>
#  1.1.1  default LIST format is now 'vsftpd' - Creality Print shows the file list with it (found with 'cycle')
#  1.1.0  --list-style (unix | unix-lf | vsftpd | unix-year | dos | names | cycle): CP's libcurl-based file list
#         did not show our files, 'cycle' tries a different LIST format on every request
#  1.0.1  --verbose also logs the directory listing that is sent for LIST / NLST
#  1.0.0  first production version, derived from ftp_probe.py 0.1.0 (any login / auto-mkdir removed)

import argparse
import os
import posixpath
import shutil
import socket
import socketserver
import threading
import time
from datetime import datetime

# ----------------------------------------------------------------- settings
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ftp_root")
WRITE_DIR = "/mmcblk0p1/creality/gztemp"       # the only place where CP may write
ALLOWED_EXT = (".gcode", ".gco")
ALLOWED_IPS = {"127.0.0.1"}                   # add the PC that runs Creality Print with --allow
MAX_UPLOAD = 300 * 1024 * 1024                 # bytes
MIN_FREE = 50 * 1024 * 1024                    # bytes that must stay free on the disk
MAX_CONN = 8
IDLE_TIMEOUT = 300                             # s, control connection
DATA_TIMEOUT = 30                              # s, data connection
VERBOSE = False
LIST_STYLES = ["unix", "unix-lf", "vsftpd", "unix-year", "dos", "names"]
LIST_STYLE = "vsftpd"                          # one of LIST_STYLES or "cycle"
_list_counter = [0]

SLOTS = threading.BoundedSemaphore(MAX_CONN)


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def vlog(*a):
    if VERBOSE:
        log(*a)


def name_ok(name):
    """file name rules for uploads / renames"""
    if not name or name.startswith(".") or len(name.encode("utf-8")) > 200:
        return False
    if any(ord(ch) < 32 or ch in '\\/:*?"<>|' for ch in name):
        return False
    return name.lower().endswith(ALLOWED_EXT)


class Handler(socketserver.StreamRequestHandler):
    timeout = IDLE_TIMEOUT

    # ------------------------------------------------------------ helpers
    def send(self, msg):
        vlog(f"{self.peer} -> {msg}")
        self.wfile.write((msg + "\r\n").encode())

    def vpath(self, arg):
        p = arg if arg.startswith("/") else posixpath.join(self.cwd, arg)
        return posixpath.normpath(p) or "/"

    def real(self, vp):
        """real path below ROOT, or None if it would leave ROOT (symlinks included)"""
        root = os.path.realpath(ROOT)
        p = os.path.realpath(os.path.join(root, vp.lstrip("/")))
        return p if os.path.commonpath([root, p]) == root else None

    def in_write_dir(self, vp):
        return posixpath.dirname(vp) == WRITE_DIR

    def open_data(self):
        try:
            if self.pasv_sock:
                self.pasv_sock.settimeout(20)
                conn, addr = self.pasv_sock.accept()
                self.pasv_sock.close()
                self.pasv_sock = None
                if addr[0] != self.client_address[0]:      # somebody else grabbed the data port
                    conn.close()
                    log(f"{self.peer} data connection from foreign address {addr[0]} refused")
                    return None
                conn.settimeout(DATA_TIMEOUT)
                return conn
            if self.port_addr:
                conn = socket.create_connection(self.port_addr, timeout=DATA_TIMEOUT)
                self.port_addr = None
                return conn
        except OSError as e:
            log(f"{self.peer} data connection failed: {e}")
        return None

    def listing(self, real_dir, names_only, style="unix"):
        lines = []
        for name in sorted(os.listdir(real_dir)):
            if name.startswith("."):                       # hides .xxx.part
                continue
            full = os.path.join(real_dir, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            isdir = os.path.isdir(full)
            t = time.localtime(st.st_mtime)
            if names_only or style == "names":
                lines.append(name)
            elif style == "vsftpd":
                lines.append("%s %4d %-8s %-8s %8d %s %s" % (
                    "drwxr-xr-x" if isdir else "-rw-r--r--", 1, "0", "0", st.st_size,
                    time.strftime("%b %d %H:%M", t), name))
            elif style == "unix-year":
                lines.append("%s 1 ftp ftp %12d %s %s" % (
                    "drwxr-xr-x" if isdir else "-rw-r--r--", st.st_size, time.strftime("%b %d  %Y", t), name))
            elif style == "dos":
                lines.append("%s %s %s" % (time.strftime("%m-%d-%y  %I:%M%p", t),
                                           "<DIR>".rjust(20) if isdir else str(st.st_size).rjust(20), name))
            else:                                          # "unix" and "unix-lf"
                lines.append("%s 1 ftp ftp %12d %s %s" % (
                    "drwxr-xr-x" if isdir else "-rw-r--r--", st.st_size, time.strftime("%b %d %H:%M", t), name))
        eol = "\n" if style == "unix-lf" and not names_only else "\r\n"
        return (eol.join(lines) + (eol if lines else "")).encode()

    # ------------------------------------------------------------ main loop
    def handle(self):
        self.peer = "%s:%s" % self.client_address
        ip = self.client_address[0]
        self.cwd, self.authed, self.user_ok = "/", False, False
        self.pasv_sock = self.port_addr = self.rename_from = None
        if ip not in ALLOWED_IPS:
            log(f"{self.peer} DENIED (not in allow-list)")
            self.send("421 not allowed")
            return
        if not SLOTS.acquire(blocking=False):
            log(f"{self.peer} DENIED (too many connections)")
            self.send("421 too many connections")
            return
        try:
            vlog(f"{self.peer} connected")
            self.send("220 cp81 ftp ready")
            while True:
                try:
                    raw = self.rfile.readline()
                except (socket.timeout, ConnectionError):
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                cmd, _, arg = line.partition(" ")
                cmd = cmd.upper()
                vlog(f"{self.peer} <- {'PASS ***' if cmd == 'PASS' else line}")
                if cmd == "QUIT":
                    self.send("221 bye")
                    break
                if not self.authed and cmd not in ("USER", "PASS", "FEAT", "OPTS", "SYST", "NOOP"):
                    self.send("530 please login first")
                    continue
                try:
                    getattr(self, "do_" + cmd, self.do_unknown)(arg)
                except Exception as e:                     # odd input must not kill the session
                    log(f"{self.peer} ERROR in {cmd}: {e!r}")
                    try:
                        self.send("451 local error")
                    except OSError:
                        break
        finally:
            if self.pasv_sock:
                self.pasv_sock.close()
            SLOTS.release()
            vlog(f"{self.peer} disconnected")

    # ------------------------------------------------------------ session commands
    def do_unknown(self, arg):
        self.send("502 command not implemented")

    def do_USER(self, arg):
        self.user_ok = arg.lower() in ("anonymous", "ftp")
        self.send("331 send e-mail address as password" if self.user_ok else "331 password required")

    def do_PASS(self, arg):
        if self.user_ok:
            self.authed = True
            self.send("230 logged in")
        else:
            log(f"{self.peer} login refused")
            self.send("530 login incorrect")

    def do_SYST(self, arg):
        self.send("215 UNIX Type: L8")

    def do_FEAT(self, arg):
        self.wfile.write(b"211-Features:\r\n PASV\r\n EPSV\r\n SIZE\r\n MDTM\r\n UTF8\r\n211 End\r\n")

    def do_OPTS(self, arg):
        self.send("200 ok")

    do_TYPE = do_MODE = do_STRU = do_NOOP = do_ALLO = do_OPTS

    def do_PWD(self, arg):
        self.send(f'257 "{self.cwd}" is the current directory')

    do_XPWD = do_PWD

    def do_CWD(self, arg):
        vp = self.vpath(arg)
        real = self.real(vp)
        if real and os.path.isdir(real):
            self.cwd = vp
            self.send(f"250 directory is now {vp}")
        else:
            self.send("550 no such directory")

    do_XCWD = do_CWD

    def do_CDUP(self, arg):
        self.do_CWD("..")

    do_XCUP = do_CDUP

    def do_SIZE(self, arg):
        real = self.real(self.vpath(arg))
        if real and os.path.isfile(real):
            self.send(f"213 {os.path.getsize(real)}")
        else:
            self.send("550 no such file")

    def do_MDTM(self, arg):
        real = self.real(self.vpath(arg))
        if real and os.path.isfile(real):
            self.send("213 " + time.strftime("%Y%m%d%H%M%S", time.gmtime(os.path.getmtime(real))))
        else:
            self.send("550 no such file")

    # ------------------------------------------------------------ data connection setup
    def _listen(self):
        if self.pasv_sock:
            self.pasv_sock.close()
        self.port_addr = None
        self.pasv_sock = socket.socket()
        ip = self.request.getsockname()[0]
        self.pasv_sock.bind((ip, 0))
        self.pasv_sock.listen(1)
        return ip, self.pasv_sock.getsockname()[1]

    def do_PASV(self, arg):
        ip, port = self._listen()
        self.send("227 Entering Passive Mode (%s,%d,%d)" % (ip.replace(".", ","), port >> 8, port & 255))

    def do_EPSV(self, arg):
        _, port = self._listen()
        self.send("229 Entering Extended Passive Mode (|||%d|)" % port)

    def do_PORT(self, arg):
        n = [int(x) for x in arg.split(",")]
        addr = (".".join(map(str, n[:4])), n[4] * 256 + n[5])
        if addr[0] != self.client_address[0]:              # no FTP bounce attacks
            self.send("501 address must be the client's own")
            return
        self.port_addr = addr
        self.send("200 PORT ok")

    # ------------------------------------------------------------ reading
    def do_LIST(self, arg):
        self._list(arg, names_only=False)

    def do_NLST(self, arg):
        self._list(arg, names_only=True)

    def _list(self, arg, names_only):
        arg = " ".join(w for w in arg.split() if not w.startswith("-"))      # ignore ls flags
        real = self.real(self.vpath(arg) if arg else self.cwd)
        if not real or not os.path.isdir(real):
            self.send("550 no such directory")
            return
        self.send("150 here comes the listing")
        conn = self.open_data()
        if not conn:
            self.send("425 cannot open data connection")
            return
        style = LIST_STYLE
        if style == "cycle" and not names_only:
            _list_counter[0] += 1
            style = LIST_STYLES[(_list_counter[0] - 1) % len(LIST_STYLES)]
            log(f"{self.peer} LIST request #{_list_counter[0]} answered in style '{style}'")
        data = self.listing(real, names_only, style)
        vlog(f"{self.peer} listing of {real} sent:", data.decode("utf-8", "replace").strip() or "(empty)")
        with conn:
            conn.sendall(data)
        self.send("226 done")

    def do_RETR(self, arg):
        real = self.real(self.vpath(arg))
        if not real or not os.path.isfile(real) or os.path.basename(real).startswith("."):
            self.send("550 no such file")
            return
        self.send("150 sending file")
        conn = self.open_data()
        if not conn:
            self.send("425 cannot open data connection")
            return
        with conn, open(real, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
        self.send("226 transfer complete")

    # ------------------------------------------------------------ writing (WRITE_DIR only)
    def do_STOR(self, arg):
        vp = self.vpath(arg)
        name = posixpath.basename(vp)
        if not self.in_write_dir(vp):
            log(f"{self.peer} upload REJECTED {vp}: writing is only allowed in {WRITE_DIR}")
            self.send("550 permission denied")
            return
        if not name_ok(name):
            log(f"{self.peer} upload REJECTED {vp}: file name / extension not allowed {ALLOWED_EXT}")
            self.send("553 file name not allowed")
            return
        real_dir = self.real(WRITE_DIR)
        if not real_dir or not os.path.isdir(real_dir):
            self.send("550 target directory missing")
            return
        if shutil.disk_usage(real_dir).free < MIN_FREE:
            log(f"{self.peer} upload REJECTED {vp}: less than {MIN_FREE // 2**20} MB free")
            self.send("552 insufficient storage")
            return
        final = os.path.join(real_dir, name)
        part = os.path.join(real_dir, "." + name + ".part")
        self.send("150 ok to send data")
        conn = self.open_data()
        if not conn:
            self.send("425 cannot open data connection")
            return
        t0, size, error = time.time(), 0, None
        try:
            with conn, open(part, "wb") as f:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        error = "552 file too large"
                        break
                    f.write(chunk)
            if error is None:
                os.replace(part, final)               # appears under its real name only now
        except OSError as e:
            error = f"426 transfer failed: {e}"
        if error:
            try:
                os.remove(part)
            except OSError:
                pass
            log(f"{self.peer} upload FAILED {vp}: {error} after {size} bytes")
            self.send(error)
            return
        log(f"{self.peer} UPLOAD {vp}  {size} bytes in {time.time() - t0:.1f}s")
        self.send("226 transfer complete")

    def do_DELE(self, arg):
        vp = self.vpath(arg)
        real = self.real(vp)
        if not self.in_write_dir(vp) or not real or not os.path.isfile(real):
            self.send("550 cannot delete")
            return
        os.remove(real)
        log(f"{self.peer} DELETED {vp}")
        self.send("250 deleted")

    def do_RNFR(self, arg):
        vp = self.vpath(arg)
        real = self.real(vp)
        if not self.in_write_dir(vp) or not real or not os.path.isfile(real):
            self.send("550 cannot rename")
            return
        self.rename_from = vp
        self.send("350 ready for RNTO")

    def do_RNTO(self, arg):
        src, dst = self.rename_from, self.vpath(arg)
        self.rename_from = None
        if not src or not self.in_write_dir(dst) or not name_ok(posixpath.basename(dst)):
            self.send("550 rename not allowed")
            return
        os.replace(self.real(src), self.real(dst))
        log(f"{self.peer} RENAMED {src} -> {dst}")
        self.send("250 renamed")

    def do_MKD(self, arg):
        self.send("550 permission denied")

    do_XMKD = do_RMD = do_XRMD = do_MKD

    def do_ABOR(self, arg):
        self.send("226 nothing to abort")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global ROOT, ALLOWED_IPS, MAX_UPLOAD, VERBOSE, LIST_STYLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--port", type=int, default=21)
    ap.add_argument("--allow", default=",".join(sorted(ALLOWED_IPS)),
                    help="comma separated client IPs that may connect")
    ap.add_argument("--max-upload-mb", type=int, default=MAX_UPLOAD // 2**20)
    ap.add_argument("--list-style", default=LIST_STYLE, choices=LIST_STYLES + ["cycle"],
                    help="format of LIST answers; 'cycle' uses another format for every request (debugging)")
    ap.add_argument("--verbose", action="store_true", help="log every FTP command")
    ap.add_argument("--version", action="version", version=f"cp81_ftp {__version__}")
    a = ap.parse_args()
    ROOT = os.path.abspath(a.root)
    ALLOWED_IPS = {x.strip() for x in a.allow.split(",") if x.strip()}
    MAX_UPLOAD = a.max_upload_mb * 2**20
    VERBOSE = a.verbose
    LIST_STYLE = a.list_style
    os.makedirs(os.path.join(ROOT, WRITE_DIR.lstrip("/")), exist_ok=True)
    log(f"cp81_ftp v{__version__} | root {ROOT} | port {a.port} | allowed {sorted(ALLOWED_IPS)} | "
        f"writes only in {WRITE_DIR} {ALLOWED_EXT}, max {a.max_upload_mb} MB")
    with Server(("0.0.0.0", a.port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
