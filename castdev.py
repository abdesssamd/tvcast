"""Google Cast (Chromecast, Android TV, la plupart des box) : decouverte mDNS et lecture d'une URL."""
import asyncio
import socket
import time
import uuid

import pychromecast
from pychromecast.config import APP_DASHCAST
from pychromecast.controllers.dashcast import DashCastController
from zeroconf import ServiceBrowser, Zeroconf

_active: dict[str, pychromecast.Chromecast] = {}


def _discover_sync(wait: float) -> list[dict]:
    zc, found = Zeroconf(), {}

    class Listener:
        def add_service(self, z, t, name):
            info = z.get_service_info(t, name, 2500)
            if info and info.parsed_addresses():
                props = {k.decode(): (v or b"").decode(errors="replace") for k, v in info.properties.items()}
                dev_id = props.get("id", name)
                found[dev_id] = dict(id="cast:" + dev_id, kind="cast", name=props.get("fn", name),
                                     maker="Google Cast", model=props.get("md", ""),
                                     ip=info.parsed_addresses()[0], port=info.port or 8009, uuid=dev_id)

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    ServiceBrowser(zc, "_googlecast._tcp.local.", Listener())
    time.sleep(wait)
    zc.close()
    return sorted(found.values(), key=lambda d: d["name"].lower())


async def discover(wait: float = 4.0) -> list[dict]:
    return await asyncio.to_thread(_discover_sync, wait)


def _probe_sync(ip: str) -> list[dict]:
    """Verifie si l'adresse IP est un appareil Google Cast (port 8009)."""
    try:
        socket.create_connection((ip, 8009), 2).close()
        cc = pychromecast.get_chromecast_from_host((ip, 8009, uuid.uuid4(), None, None), tries=1, timeout=5)
        cc.wait(timeout=6)
        info = cc.cast_info
        dev_id = info.uuid.hex if info.uuid else ip
        d = dict(id="cast:" + dev_id, kind="cast", name=cc.name or ip, maker="Google Cast",
                 model=info.model_name or "", ip=ip, port=8009, uuid=dev_id)
        cc.disconnect()
        return [d]
    except Exception:
        return []


async def probe(ip: str) -> list[dict]:
    return await asyncio.to_thread(_probe_sync, ip)


def _connect(dev: dict) -> pychromecast.Chromecast:
    try:
        u = uuid.UUID(dev["uuid"])
    except ValueError:
        u = uuid.uuid4()
    cc = pychromecast.get_chromecast_from_host((dev["ip"], dev["port"], u, dev["model"], dev["name"]))
    cc.wait(timeout=10)
    return cc


def _cast_sync(dev: dict, url: str):
    cc = _connect(dev)
    mc = cc.media_controller
    mc.play_media(url, "video/mp4", stream_type="LIVE")
    mc.block_until_active(timeout=10)
    _active[dev["id"]] = cc


def _page_sync(dev: dict, url: str):
    """Ouvre une page web sur la box (appli DashCast) : sert au lecteur temps reel de MultiTV."""
    cc = _connect(dev)
    ctrl = DashCastController()
    cc.register_handler(ctrl)
    ctrl.load_url(url, force=True)
    for _ in range(50):
        if cc.app_id == APP_DASHCAST:
            _active[dev["id"]] = cc
            return
        time.sleep(0.2)
    cc.disconnect()
    raise RuntimeError("DashCast indisponible sur cette box")


def _stop_sync(dev: dict):
    cc = _active.pop(dev["id"], None)
    if cc:
        cc.quit_app()
        cc.disconnect()


async def cast(dev: dict, url: str):
    await asyncio.to_thread(_stop_sync, dev)
    await asyncio.to_thread(_cast_sync, dev, url)


async def stop(dev: dict):
    await asyncio.to_thread(_stop_sync, dev)


async def cast_page(dev: dict, url: str):
    await asyncio.to_thread(_stop_sync, dev)
    await asyncio.to_thread(_page_sync, dev, url)
