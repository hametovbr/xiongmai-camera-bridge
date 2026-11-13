"""Minimal DVRIP ("Sofia") client for Xiongmai cameras, TCP port 34567.

Every message is a 20-byte little-endian header followed by a JSON payload:

    offset size  field
    0      1     head      0xFF
    1      1     version   0x01
    2      2     reserved  0x0000
    4      4     session   session id (0 before login)
    8      4     sequence  incremented by the client
    12     1     total     fragment count (0 = not fragmented)
    13     1     current   fragment index
    14     2     msg_id    message type, e.g. 1000 = login
    16     4     length    payload length in bytes
    20     ...   payload   JSON, usually terminated with "\\n\\0"

A reply uses msg_id + 1 (login 1000 -> 1001, config get 1042 -> 1043, ...).
The password is not sent in clear text: it is hashed with the vendor "sofia"
hash (MD5, then 8 characters derived from pairs of digest bytes), see xmmd5().

Message types used here:

    1000/1001  login
    1040/1041  set config
    1042/1043  get config
    1400/1401  OPPTZControl (pan/tilt, presets)
    1450/1451  OPMachine (reboot)
"""

import hashlib
import json
import socket
import struct

VERSION = 0x01
MAX_PAYLOAD = 32768
HEADER = struct.Struct("<BBxxIIBBHI")   # "<BBHIIBBHI" reads the same bytes

LOGIN = 1000
SET_CONFIG = 1040
GET_CONFIG = 1042
PTZ_CONTROL = 1400
MACHINE = 1450

RET_OK = 100            # also returned for unknown PTZ commands: not a proof of action
RET_NEED_REBOOT = 603   # config accepted, applied after reboot
RET_NO_SECTION = 607    # config section does not exist on this firmware


def xmmd5(password: str) -> str:
    """Xiongmai "sofia" password hash: 8 chars from pairs of MD5 digest bytes."""
    digest = hashlib.md5(password.encode("utf-8")).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[(digest[2 * i] + digest[2 * i + 1]) % 62] for i in range(8))


class DvripError(RuntimeError):
    pass


class Dvrip:
    """One DVRIP session: login on construction, close() when done.

    The camera drops idle sessions and reboots on some config writes, so the
    bridge opens a fresh session per command instead of keeping one alive.
    """

    def __init__(self, host, user, password, port=34567, timeout=8.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.session = 0
        self.sequence = 0
        self.sid = None
        rep = self.request(LOGIN, {"UserName": user, "PassWord": xmmd5(password),
                                   "EncryptType": "MD5", "LoginType": "DVRIP-Web"})
        if rep.get("Ret") != RET_OK:
            self.close()
            raise DvripError(f"login failed: Ret={rep.get('Ret')}")
        self.sid = rep["SessionID"]
        self.session = int(self.sid, 16)

    # -- transport

    def _send(self, msg_id, obj):
        payload = json.dumps(obj).encode("utf-8") + b"\x0a\x00"
        self.sequence += 1
        self.sock.sendall(HEADER.pack(0xFF, VERSION, self.session, self.sequence,
                                      0, 0, msg_id, len(payload)) + payload)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise DvripError("connection closed by camera")
            buf += chunk
        return buf

    def _recv(self, want):
        while True:
            head = HEADER.unpack(self._recv_exact(HEADER.size))
            msg_id, length = head[6], head[7]   # xx padding is not returned
            body = self._recv_exact(length)
            if msg_id != want:
                continue  # unsolicited message (alarm, keep-alive): skip it
            raw = body.rstrip(b"\x00\n\r \\")
            return json.loads(raw.decode("utf-8", "replace")) if raw else {}

    def request(self, msg_id, obj):
        self._send(msg_id, obj)
        return self._recv(msg_id + 1)

    # -- API

    def get_config(self, name):
        rep = self.request(GET_CONFIG, {"Name": name, "SessionID": self.sid})
        if rep.get("Ret") != RET_OK:
            raise DvripError(f"get {name}: Ret={rep.get('Ret')}")
        return rep[name]

    def set_config(self, name, value):
        rep = self.request(SET_CONFIG, {"Name": name, "SessionID": self.sid, name: value})
        if rep.get("Ret") not in (RET_OK, RET_NEED_REBOOT):
            raise DvripError(f"set {name}: Ret={rep.get('Ret')}")
        return rep.get("Ret")

    def ptz(self, command, preset=65535, step=1):
        """Send one OPPTZControl command.

        command  exact DVRIP name, case-sensitive: DirectionLeft, GotoPreset, ...
        preset   slot number for presets; for Direction* 0/65535 = start, -1 = stop
        step     speed for Direction* (not a number of steps)
        """
        rep = self.request(PTZ_CONTROL, {
            "Name": "OPPTZControl", "SessionID": self.sid,
            "OPPTZControl": {"Command": command, "Parameter": {
                "Channel": 0, "Preset": preset, "Step": step,
                "Tour": 0, "Pattern": 0, "MenuOpt": 0, "Aux": 0}}})
        if rep.get("Ret") != RET_OK:
            raise DvripError(f"ptz {command}: Ret={rep.get('Ret')}")
        return rep.get("Ret")

    def reboot(self):
        return self.request(MACHINE, {"Name": "OPMachine", "SessionID": self.sid,
                                      "OPMachine": {"Action": "Reboot"}})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
