#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Required Notice: Copyright (c) 2026 NewComerDS (https://github.com/NewComerDS/LAN-Creality-CR-10_V2)
"""
ftp_probe.py - tiny FTP server for finding out how Creality Print uploads G-code
to an "old" printer (CP connects to port 21).

* accepts ANY user / password and LOGS them (together with every command),
* missing directories are created on CWD / STOR (probe only - helps to see the path CP wants),
* everything lives under --root; nothing outside it can be touched,
* standard library only, passive (PASV/EPSV) and active (PORT) mode.

Run (port 21 needs root):
    sudo python3 tools/ftp_probe.py --root ./ftp_root
Then in Creality Print: send / upload a small .gcode to the printer and send me the log.
"""
__version__ = "0.1.0"

import argparse
import os
import posixpath
import socket
import socketserver
import time
from datetime import datetime

ROOT = "./ftp_root"
PRECREATE = ["media/mmcblk0p1/creality/gztemp"]      # the path CP uses in print=... commands


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


class Handler(socketserver.StreamRequestHandler):

    # ------------------------------------------------------------ helpers
    def send(self, msg):
        log(f"{self.peer} -> {msg}")
        self.wfile.write((msg + "\r\n").encode())

    def vpath(self, arg):
        """virtual absolute path, can never leave '/'"""
        p = arg if arg.startswith("/") else posixpath.join(self.cwd, arg)
        return posixpath.normpath(p) or "/"

    def real(self, vp):
        return os.path.join(ROOT, vp.lstrip("/"))

    def open_data(self):
        try:
            if self.pasv_sock:
                self.pasv_sock.settimeout(20)
                conn, _ = self.pasv_sock.accept()
                self.pasv_sock.close()
                self.pasv_sock = None
                return conn
            if self.port_addr:
                conn = socket.create_connection(self.port_addr, timeout=20)
                self.port_addr = None
                return conn
        except OSError as e:
            log(f"{self.peer} data connection failed: {e}")
        return None

    def listing(self, real_dir, names_only):
        lines = []
        for name in sorted(os.listdir(real_dir)):
            full = os.path.join(real_dir, name)
            st = os.stat(full)
            if names_only:
                lines.append(name)
            else:
                kind = "d" if os.path.isdir(full) else "-"
                when = time.strftime("%b %d %H:%M", time.localtime(st.st_mtime))
                lines.append(f"{kind}rw-r--r-- 1 ftp ftp {st.st_size:>12} {when} {name}")
        return ("\r\n".join(lines) + ("\r\n" if lines else "")).encode()

    # ------------------------------------------------------------ main loop
    def handle(self):
        self.peer = "%s:%s" % self.client_address
        self.cwd = "/"
        self.pasv_sock = None
        self.port_addr = None
        self.rename_from = None
        log(f"{self.peer} connected")
        self.send("220 ftp_probe ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            log(f"{self.peer} <- {line}")
            try:
                if cmd == "QUIT":
                    self.send("221 bye")
                    break
                getattr(self, "do_" + cmd, self.do_unknown)(arg)
            except Exception as e:                       # never kill the server on odd input
                log(f"{self.peer} ERROR in {cmd}: {e!r}")
                self.send("451 local error")
        log(f"{self.peer} disconnected")

    # ------------------------------------------------------------ commands
    def do_unknown(self, arg):
        self.send("502 command not implemented")

    def do_USER(self, arg):
        self.send("331 any password will do")

    def do_PASS(self, arg):
        self.send("230 logged in")

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
        os.makedirs(self.real(vp), exist_ok=True)         # probe: create what CP asks for
        self.cwd = vp
        self.send(f"250 directory is now {vp}")

    do_XCWD = do_CWD

    def do_CDUP(self, arg):
        self.do_CWD("..")

    do_XCUP = do_CDUP

    def do_MKD(self, arg):
        vp = self.vpath(arg)
        os.makedirs(self.real(vp), exist_ok=True)
        self.send(f'257 "{vp}" created')

    do_XMKD = do_MKD

    def do_RMD(self, arg):
        try:
            os.rmdir(self.real(self.vpath(arg)))
            self.send("250 removed")
        except OSError:
            self.send("550 cannot remove")

    do_XRMD = do_RMD

    def do_DELE(self, arg):
        try:
            os.remove(self.real(self.vpath(arg)))
            self.send("250 deleted")
        except OSError:
            self.send("550 cannot delete")

    def do_RNFR(self, arg):
        self.rename_from = self.vpath(arg)
        self.send("350 ready for RNTO")

    def do_RNTO(self, arg):
        try:
            os.replace(self.real(self.rename_from), self.real(self.vpath(arg)))
            self.send("250 renamed")
        except (OSError, TypeError):
            self.send("550 rename failed")

    def do_SIZE(self, arg):
        try:
            self.send(f"213 {os.path.getsize(self.real(self.vpath(arg)))}")
        except OSError:
            self.send("550 no such file")

    def do_MDTM(self, arg):
        try:
            t = os.path.getmtime(self.real(self.vpath(arg)))
            self.send("213 " + time.strftime("%Y%m%d%H%M%S", time.gmtime(t)))
        except OSError:
            self.send("550 no such file")

    def do_PASV(self, arg):
        if self.pasv_sock:
            self.pasv_sock.close()
        self.port_addr = None
        self.pasv_sock = socket.socket()
        ip = self.request.getsockname()[0]
        self.pasv_sock.bind((ip, 0))
        self.pasv_sock.listen(1)
        port = self.pasv_sock.getsockname()[1]
        self.send("227 Entering Passive Mode (%s,%d,%d)" % (ip.replace(".", ","), port >> 8, port & 255))

    def do_EPSV(self, arg):
        if self.pasv_sock:
            self.pasv_sock.close()
        self.port_addr = None
        self.pasv_sock = socket.socket()
        self.pasv_sock.bind((self.request.getsockname()[0], 0))
        self.pasv_sock.listen(1)
        self.send("229 Entering Extended Passive Mode (|||%d|)" % self.pasv_sock.getsockname()[1])

    def do_PORT(self, arg):
        n = [int(x) for x in arg.split(",")]
        self.port_addr = (".".join(map(str, n[:4])), n[4] * 256 + n[5])
        self.send("200 PORT ok")

    def do_LIST(self, arg):
        self._list(arg, names_only=False)

    def do_NLST(self, arg):
        self._list(arg, names_only=True)

    def _list(self, arg, names_only):
        arg = " ".join(w for w in arg.split() if not w.startswith("-"))   # ignore ls flags
        vp = self.vpath(arg) if arg else self.cwd
        real = self.real(vp)
        if not os.path.isdir(real):
            self.send("550 no such directory")
            return
        self.send("150 here comes the listing")
        conn = self.open_data()
        if not conn:
            self.send("425 cannot open data connection")
            return
        with conn:
            conn.sendall(self.listing(real, names_only))
        self.send("226 done")

    def do_RETR(self, arg):
        real = self.real(self.vpath(arg))
        if not os.path.isfile(real):
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

    def do_STOR(self, arg):
        vp = self.vpath(arg)
        real = self.real(vp)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        self.send("150 ok to send data")
        conn = self.open_data()
        if not conn:
            self.send("425 cannot open data connection")
            return
        t0, size = time.time(), 0
        with conn, open(real, "wb") as f:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)
        log(f"{self.peer} *** UPLOAD stored {vp}  {size} bytes in {time.time() - t0:.1f}s  -> {real}")
        self.send("226 transfer complete")

    do_APPE = do_STOR

    def do_ABOR(self, arg):
        self.send("226 nothing to abort")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--port", type=int, default=21)
    ap.add_argument("--version", action="version", version=f"ftp_probe {__version__}")
    a = ap.parse_args()
    ROOT = os.path.abspath(a.root)
    for d in PRECREATE:
        os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    log(f"ftp_probe v{__version__} | root {ROOT} | port {a.port}")
    with Server(("0.0.0.0", a.port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
