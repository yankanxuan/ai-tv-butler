"""DLNA/UPnP MediaRenderer control for casting media to the TV.

The TV runs a standard UPnP AV MediaRenderer (Platinum stack), which the fnOS TV
app also uses. Casting a URL through AVTransport makes the TV play it in its own
player, so playback history/UI stay consistent — unlike handing the URL to a
separate system player via an Intent.

Only the AVTransport actions needed for casting are implemented: set the URI,
start, pause, stop, seek and query position.
"""
from __future__ import annotations

import re
import socket
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

# 用于探测本机出口网卡；UDP connect 不发包，离线也安全。
_PROBE_HOST = "8.8.8.8"

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
DIDL = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
    "<item id=\"0\" parentID=\"-1\" restricted=\"1\">"
    "<dc:title>{title}</dc:title>"
    "<upnp:class>object.item.videoItem</upnp:class>"
    "<res protocolInfo=\"http-get:*:video/mp4:*\">{url}</res>"
    "</item></DIDL-Lite>"
)


@dataclass
class Renderer:
    location: str
    friendly_name: str
    av_control: str          # absolute control URL
    udn: str = ""


def _local_ip(target: str = _PROBE_HOST) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((target, 9))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def discover(timeout: float = 3.0, targets: list[str] | None = None) -> list[Renderer]:
    """SSDP-discover MediaRenderers on the LAN (optionally only `targets`)."""
    message = "\r\n".join([
        "M-SEARCH * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        'MAN: "ssdp:discover"',
        "MX: 2",
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1",
        "", "",
    ]).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    try:
        sock.sendto(message, ("239.255.255.250", 1900))
    except OSError:
        return []
    found: dict[str, Renderer] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, address = sock.recvfrom(8192)
        except socket.timeout:
            continue
        except OSError:
            break
        if targets and address[0] not in targets:
            continue
        text = data.decode("latin1", "ignore")
        location = re.search(r"^Location:\s*(\S+)", text, re.M | re.I)
        if not location:
            continue
        renderer = describe(location.group(1).strip())
        if renderer is not None:
            found[renderer.av_control] = renderer
    sock.close()
    return list(found.values())


def describe(location: str) -> Renderer | None:
    try:
        with urllib.request.urlopen(location, timeout=5) as response:
            xml = response.read().decode("utf-8", "ignore")
    except OSError:
        return None
    root = ET.fromstring(xml)
    ns = {"u": "urn:schemas-upnp-org:device-1-0"}
    name = root.findtext(".//u:device/u:friendlyName", default="", namespaces=ns)
    udn = root.findtext(".//u:device/u:UDN", default="", namespaces=ns)
    for service in root.findall(".//u:service", ns):
        service_type = service.findtext("u:serviceType", default="", namespaces=ns)
        if service_type.strip() != AVT:
            continue
        control = service.findtext("u:controlURL", default="", namespaces=ns)
        if not control:
            continue
        base = re.match(r"(https?://[^/]+)", location)
        if control.startswith("http"):
            url = control
        elif control.startswith("/"):
            url = (base.group(1) if base else "") + control
        else:
            url = location.rsplit("/", 1)[0] + "/" + control
        return Renderer(location=location, friendly_name=name, av_control=url, udn=udn)
    return None


class CastClient:
    def __init__(self, renderer: Renderer, timeout: float = 8.0):
        self.renderer = renderer
        self.timeout = timeout

    # ---------- SOAP ----------
    def _soap(self, action: str, arguments: dict[str, str]) -> str:
        body = "".join(f"<{key}>{_escape(value)}</{key}>"
                       for key, value in arguments.items())
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f"<s:Body><u:{action} xmlns:u=\"{AVT}\">{body}</u:{action}></s:Body>"
            "</s:Envelope>"
        )
        request = urllib.request.Request(
            self.renderer.av_control, data=envelope.encode(),
            headers={"Content-Type": 'text/xml; charset="utf-8"',
                     "SOAPAction": f'"{AVT}#{action}"'},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"DLNA {action} 失败 HTTP {exc.code}: {detail}") from exc
        except OSError as exc:
            raise RuntimeError(f"DLNA {action} 请求失败: {exc}") from exc

    # ---------- 播放控制 ----------
    def set_uri(self, url: str, title: str = "") -> str:
        metadata = DIDL.format(title=_escape(title or "video"), url=_escape(url))
        return self._soap("SetAVTransportURI",
                          {"InstanceID": "0", "CurrentURI": url,
                           "CurrentURIMetaData": metadata})

    def play(self, speed: str = "1") -> str:
        return self._soap("Play", {"InstanceID": "0", "Speed": speed})

    def pause(self) -> str:
        return self._soap("Pause", {"InstanceID": "0"})

    def stop(self) -> str:
        return self._soap("Stop", {"InstanceID": "0"})

    def seek(self, seconds: float) -> str:
        stamp = _hms(seconds)
        return self._soap("Seek", {"InstanceID": "0", "Unit": "REL_TIME",
                                   "Target": stamp})

    def position(self) -> dict[str, Any]:
        xml = self._soap("GetPositionInfo", {"InstanceID": "0"})
        return {"position": _parse_time(_tag(xml, "RelTime")),
                "duration": _parse_time(_tag(xml, "TrackDuration")),
                "uri": _tag(xml, "TrackURI"),
                "raw": xml[:400]}

    def transport_state(self) -> str:
        xml = self._soap("GetTransportInfo", {"InstanceID": "0"})
        return _tag(xml, "CurrentTransportState")

    def cast(self, url: str, title: str = "", start: bool = True) -> dict[str, Any]:
        """Point the renderer at `url` and start playback."""
        self.set_uri(url, title)
        if start:
            self.play()
        return {"ok": True, "renderer": self.renderer.friendly_name,
                "control": self.renderer.av_control, "url": url,
                "state": self.transport_state()}


def _escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))


def _tag(xml: str, name: str) -> str:
    match = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
    return match.group(1).strip() if match else ""


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _parse_time(value: str) -> float | None:
    match = re.match(r"(\d+):(\d{2}):(\d{2})", value or "")
    if not match:
        return None
    hours, minutes, secs = map(int, match.groups())
    return hours * 3600 + minutes * 60 + secs


def find_renderer(host: str, timeout: float = 4.0) -> Renderer | None:
    """Prefer the renderer published by `host`; fall back to any discovered."""
    found = discover(timeout=timeout, targets=[host])
    if found:
        return found[0]
    found = discover(timeout=timeout)
    return found[0] if found else None
