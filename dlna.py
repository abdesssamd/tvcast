"""Decouverte (SSDP) et pilotage (UPnP AVTransport) des TV / lecteurs DLNA du reseau local."""
import asyncio
import re
import socket
import xml.etree.ElementTree as ET
from html import escape
from urllib.parse import urljoin, urlparse

import aiohttp

CRLF = chr(13) + chr(10)
SSDP = ("239.255.255.250", 1900)
TARGETS = ("urn:schemas-upnp-org:device:MediaRenderer:1", "urn:schemas-upnp-org:service:AVTransport:1")
DIDL = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1"><dc:title>MultiTV</dc:title>'
        '<upnp:class>object.item.videoItem</upnp:class>'
        '<res protocolInfo="http-get:*:video/mpeg:*">{url}</res></item></DIDL-Lite>')


class _Collector(asyncio.DatagramProtocol):
    def __init__(self):
        self.locations: set[str] = set()

    def datagram_received(self, data, addr):
        m = re.search(rb"(?im)^location:\s*(\S+)", data)
        if m:
            self.locations.add(m.group(1).decode(errors="replace"))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


async def _describe(session: aiohttp.ClientSession, location: str) -> dict | None:
    try:
        async with session.get(location, timeout=aiohttp.ClientTimeout(total=4)) as r:
            root = ET.fromstring(await r.read())
    except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError, ValueError):
        return None
    dev = next((e for e in root.iter() if _local(e.tag) == "device"), None)
    if dev is None:
        return None
    info = {_local(c.tag): (c.text or "").strip() for c in dev if len(c) == 0}
    for svc in root.iter():
        if _local(svc.tag) == "service":
            f = {_local(c.tag): (c.text or "").strip() for c in svc}
            if "AVTransport" in f.get("serviceType", ""):
                return {
                    "id": info.get("UDN", location),
                    "name": info.get("friendlyName", "Appareil"),
                    "maker": info.get("manufacturer", ""),
                    "model": info.get("modelName", ""),
                    "ip": urlparse(location).hostname,
                    "control": urljoin(location, f["controlURL"]),
                    "stype": f["serviceType"],
                }
    return None   # pas un lecteur video (routeur, NAS...)


def local_ips() -> list[str]:
    """Toutes les adresses IPv4 du PC (Wi-Fi, Ethernet, VPN...) : les TV peuvent etre sur n'importe laquelle."""
    ips = set()
    try:
        ips.update(r[4][0] for r in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith(("127.", "169.254.")))


async def _search(bind_ip: str, dest: tuple[str, int], wait: float) -> set[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
        sock.bind((bind_ip, 0))
        sock.setblocking(False)
    except OSError:
        sock.close()
        return set()
    transport, proto = await asyncio.get_running_loop().create_datagram_endpoint(_Collector, sock=sock)
    try:
        for _ in range(2):
            for st in TARGETS + ("upnp:rootdevice",):
                lines = ["M-SEARCH * HTTP/1.1", f"HOST: {dest[0]}:{dest[1]}", 'MAN: "ssdp:discover"',
                         "MX: 2", f"ST: {st}", "", ""]
                transport.sendto(CRLF.join(lines).encode(), dest)
            await asyncio.sleep(wait / 2)
    finally:
        transport.close()
    return proto.locations


async def _describe_all(locations: set[str]) -> list[dict]:
    async with aiohttp.ClientSession() as s:
        found = await asyncio.gather(*(_describe(s, u) for u in locations))
    unique = {d["id"]: dict(d, kind="dlna") for d in found if d}
    return sorted(unique.values(), key=lambda d: d["name"].lower())


async def discover(timeout: float = 3.0) -> list[dict]:
    """Recherche multicast sur chaque interface reseau du PC."""
    found = await asyncio.gather(*(_search(ip, SSDP, timeout) for ip in local_ips()))
    return await _describe_all(set().union(*found))


async def probe(ip: str) -> list[dict]:
    """Interroge directement une adresse IP (utile quand le multicast est bloque par le routeur/Wi-Fi)."""
    return await _describe_all(await _search("0.0.0.0", (ip, 1900), 2.0))


async def _soap(dev: dict, action: str, args: str = ""):
    body = (f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{dev["stype"]}"><InstanceID>0</InstanceID>{args}</u:{action}></s:Body></s:Envelope>')
    headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{dev["stype"]}#{action}"'}
    async with aiohttp.ClientSession() as s:
        async with s.post(dev["control"], data=body.encode(), headers=headers,
                          timeout=aiohttp.ClientTimeout(total=8)) as r:
            text = await r.text()
            if r.status >= 400:
                err = re.search(r"<errorDescription>(.*?)</errorDescription>", text)
                raise RuntimeError(err.group(1) if err else f"{action}: HTTP {r.status}")


async def cast(dev: dict, url: str):
    try:
        await _soap(dev, "Stop")
    except Exception:
        pass
    await _soap(dev, "SetAVTransportURI",
                f"<CurrentURI>{escape(url)}</CurrentURI>"
                f"<CurrentURIMetaData>{escape(DIDL.format(url=escape(url)))}</CurrentURIMetaData>")
    await _soap(dev, "Play", "<Speed>1</Speed>")


async def stop(dev: dict):
    await _soap(dev, "Stop")
