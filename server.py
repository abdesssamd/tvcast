"""MultiTV - capture l'ecran du PC et le diffuse (HLS) vers plusieurs Smart TV.

Lancer :  python server.py   puis ouvrir http://localhost:8080/admin
"""
import asyncio
import json
import os
import shutil
import socket
import sys
import time
import webbrowser
from pathlib import Path

import imageio_ffmpeg
import segno
from aiohttp import WSMsgType, web

import capture
import castdev
import diag
import dlna

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).parent if FROZEN else Path(__file__).parent   # donnees a cote du .exe
STATIC = Path(getattr(sys, "_MEIPASS", ROOT)) / "static"
STREAM = ROOT / "stream"
STORE = ROOT / "tvs.json"
PORT = int(os.environ.get("MULTITV_PORT", 8765))

PRESETS = {
    "720p30": dict(height=720, fps=30, bitrate=3000),
    "1080p30": dict(height=1080, fps=30, bitrate=6000),
    "1080p60": dict(height=1080, fps=60, bitrate=9000),
}
# Debit constant : une TV remplit son tampon a vitesse reguliere, meme quand l'ecran est immobile.
ENCODERS = {
    "libx264": ["-preset", "ultrafast", "-tune", "zerolatency", "-x264-params", "nal-hrd=cbr:force-cfr=1"],
    "h264_nvenc": ["-preset", "p1", "-tune", "ll", "-rc", "cbr", "-zerolatency", "1"],
    "h264_qsv": ["-preset", "veryfast", "-look_ahead", "0"],
    "h264_amf": ["-usage", "ultralowlatency", "-rc", "cbr"],
}
LOCAL_ONLY = ("/admin", "/ws/admin")


def local_ip(target: str = "10.255.255.255") -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def codec_of(init: bytes) -> str:
    i = init.find(b"avcC")
    return "avc1.%02X%02X%02X" % tuple(init[i + 5:i + 8]) if i > 0 else "avc1.4D401F"


def is_keyframe(moof: bytes) -> bool:
    """Le fragment commence-t-il par une image cle ? (flags du premier echantillon du 'trun')"""
    i = moof.find(b"trun")
    if i < 0:
        return False
    flags = int.from_bytes(moof[i + 5:i + 8], "big")
    p = i + 12
    if flags & 0x1:
        p += 4
    if not flags & 0x4:
        if not flags & 0x400:
            return False
        p += 4 * bool(flags & 0x100) + 4 * bool(flags & 0x200)
    return not int.from_bytes(moof[p:p + 4], "big") & 0x10000


class Streamer:
    """Un seul processus FFmpeg : capture -> H.264 -> MP4 fragmente (WebSocket) ou HLS."""

    def __init__(self):
        self.config = dict(PRESETS["1080p30"], encoder="libx264", mode="live", source={"kind": "desktop"})
        self.proc: asyncio.subprocess.Process | None = None
        self.error = ""
        self.init = b""
        self.codec = ""

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _input(self) -> list[str]:
        """Source de capture : tout l'ecran, une fenetre (suivie si elle bouge) ou une zone fixe."""
        src = self.config["source"]
        if src["kind"] == "window":
            return ["-i", "title=" + src["title"]]
        if src["kind"] == "monitor":     # ecran precis : on relit sa position actuelle (elle peut changer)
            m = next((m for m in capture.list_monitors() if m["device"] == src["device"]), src)
            return ["-offset_x", str(m["x"]), "-offset_y", str(m["y"]),
                    "-video_size", f"{m['w']}x{m['h']}", "-i", "desktop"]
        if src["kind"] == "region":
            return ["-offset_x", str(src["x"]), "-offset_y", str(src["y"]),
                    "-video_size", f"{src['w']}x{src['h']}", "-i", "desktop"]
        return ["-i", "desktop"]

    def _cmd(self) -> list[str]:
        c = self.config
        fps, h, br = int(c["fps"]), int(c["height"]), int(c["bitrate"])
        mode = c["mode"]
        gop = fps if mode == "hls" else max(1, fps // 2)
        if mode == "live":
            out = ["-f", "mp4", "-movflags", "empty_moov+default_base_moof+frag_keyframe",
                   "-frag_duration", "100000", "-flush_packets", "1", "pipe:1"]
        elif mode == "ts":
            out = ["-f", "mpegts", "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
                   "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1", "pipe:1"]
        else:
            out = (["-f", "hls", "-hls_time", "1", "-hls_list_size", "5",
                "-hls_flags", "delete_segments+independent_segments+omit_endlist",
                "-hls_segment_filename", str(STREAM / "seg_%05d.ts"), str(STREAM / "index.m3u8")])
        return [
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "warning",
            "-fflags", "nobuffer",
            "-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1", *self._input(),
            "-vf", f"scale=-2:'trunc(min(ih,{h})/2)*2':flags=fast_bilinear,format=yuv420p",
            "-c:v", c["encoder"], *ENCODERS[c["encoder"]], "-profile:v", "main", "-bf", "0",
            "-b:v", f"{br}k", "-minrate", f"{br}k", "-maxrate", f"{br}k", "-bufsize", f"{br // 4}k",
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            *out,
        ]

    async def start(self):
        if self.running:
            return
        shutil.rmtree(STREAM, ignore_errors=True)
        STREAM.mkdir(exist_ok=True)
        self.error, self.init = "", b""
        mode = self.config["mode"]
        self.proc = await asyncio.create_subprocess_exec(
            *self._cmd(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL if mode == "hls" else asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, creationflags=0x08000000)   # sans fenetre console
        await hub.send_mode()
        asyncio.create_task(self._monitor(self.proc))
        if mode == "live":
            asyncio.create_task(self._pump(self.proc))
        elif mode == "ts":
            asyncio.create_task(self._pump_ts(self.proc))

    async def _pump(self, proc):
        """Decoupe la sortie MP4 en boites : init (ftyp+moov) puis fragments (moof+mdat)."""
        r, init, moof = proc.stdout, b"", b""
        try:
            while True:
                hdr = await r.readexactly(8)
                box = hdr + await r.readexactly(int.from_bytes(hdr[:4], "big") - 8)
                kind = hdr[4:8]
                if kind in (b"ftyp", b"moov"):
                    init += box
                    if kind == b"moov":
                        self.init, self.codec, init = init, codec_of(init), b""
                        await hub.send_init()
                        if hub.ts_clients:
                            await remux.ensure()
                elif kind == b"moof":
                    moof = box
                elif kind == b"mdat" and moof:
                    hub.push(moof + box, is_keyframe(moof))
                    moof = b""
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            hub.close_mp4()

    async def _pump_ts(self, proc):
        """Flux MPEG-TS brut, copie vers chaque appareil qui le lit sur /live.ts."""
        try:
            while chunk := await proc.stdout.read(65536):
                hub.push_ts(chunk)
        finally:
            hub.close_ts()

    async def _monitor(self, proc):
        err = await proc.stderr.read()
        await proc.wait()
        if proc is self.proc and proc.returncode not in (0, None):
            self.error = err.decode(errors="replace").strip()[-400:] or f"ffmpeg code {proc.returncode}"
        if proc is self.proc:
            for d in hub.devices.values():
                d["casting"] = False
        await hub.broadcast_state()

    async def stop(self, release: bool = True):
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        await remux.stop()
        hub.close_mp4()
        await hub.send_mode()
        if release:      # arret demande : on libere les appareils (pas lors d'un simple redemarrage)
            for d in hub.devices.values():
                if d.get("casting"):
                    asyncio.create_task(stop_cast(d["id"]))


class Hub:
    """Etat des TV connectees + tableau de bord."""

    def __init__(self):
        self.tvs: dict[str, dict] = {}      # id -> info (ws=None si hors ligne)
        self.admins: set[web.WebSocketResponse] = set()
        self.names: dict[str, str] = {}
        self.devices: dict[str, dict] = {}   # appareils DLNA trouves sur le reseau
        self.scanning = False
        self.ts_clients: set[asyncio.Queue] = set()
        self.mp4_clients: list[dict] = []     # lecteurs Google Cast (MP4 fragmente en HTTP)
        self.notice = ""
        if STORE.exists():
            try:
                self.names = json.loads(STORE.read_text("utf-8"))
            except ValueError:
                pass

    def save(self):
        STORE.write_text(json.dumps(self.names, ensure_ascii=False, indent=1), "utf-8")

    def state(self) -> dict:
        s = streamer
        return {
            "type": "state",
            "stream": {"running": s.running, "config": s.config, "error": s.error},
            "presets": PRESETS, "encoders": list(ENCODERS), "scanning": self.scanning,
            "notice": self.notice, "networks": dlna.local_ips(),
            "tv_url": f"http://{local_ip()}:{PORT}/tv",
            "devices": [{k: d.get(k, "") for k in ("id", "name", "maker", "model", "ip", "kind", "casting", "error", "via")}
                        for d in self.devices.values()],
            "tvs": [
                {"id": i, "name": self.names.get(i, "TV " + i[:4]), "ip": t["ip"], "ua": t["ua"],
                 "online": t["ws"] is not None and not t["ws"].closed,
                 "playing": t["playing"], "seen": t["seen"]}
                for i, t in self.tvs.items()
            ],
        }

    def _live_tvs(self):
        return [t for t in self.tvs.values() if t["ws"] is not None and not t["ws"].closed]

    @staticmethod
    def _flush(t):
        while not t["q"].empty():
            t["q"].get_nowait()
        t["synced"] = False

    def _mode_msg(self) -> str:
        return json.dumps({"type": "mode", "mode": streamer.config["mode"], "running": streamer.running})

    async def send_mode(self):
        for t in self._live_tvs():
            self._flush(t)
            t["q"].put_nowait(self._mode_msg())

    async def send_init(self):
        """Nouvelle session FFmpeg : chaque TV doit reconstruire son lecteur."""
        for t in self._live_tvs():
            self._flush(t)
            t["q"].put_nowait(json.dumps({"type": "init", "codec": streamer.codec}))
            t["q"].put_nowait(streamer.init)

    def push_ts(self, chunk: bytes):
        for q in list(self.ts_clients):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:   # lecteur trop lent : on coupe plutot que corrompre le flux
                self.ts_clients.discard(q)
                q.put_nowait(None) if not q.full() else q.get_nowait()

    def close_ts(self):
        for q in list(self.ts_clients):
            while q.full():
                q.get_nowait()
            q.put_nowait(None)
        self.ts_clients.clear()

    def close_mp4(self):
        for c in self.mp4_clients:
            while c["q"].full():
                c["q"].get_nowait()
            c["q"].put_nowait(None)
        self.mp4_clients.clear()

    def push(self, frag: bytes, key: bool):
        for c in list(self.mp4_clients):
            if not c["synced"]:
                if not key:
                    continue
                c["synced"] = True
            try:
                c["q"].put_nowait(frag)
            except asyncio.QueueFull:
                self.mp4_clients.remove(c)
                while c["q"].full():
                    c["q"].get_nowait()
                c["q"].put_nowait(None)
        remux.feed(frag, key)
        for t in self._live_tvs():
            if not t["synced"]:
                if not key:
                    continue          # une TV ne peut rejoindre qu'a une image cle
                t["synced"] = True
            try:
                t["q"].put_nowait(frag)
            except asyncio.QueueFull:  # TV trop lente : on saute jusqu'a la prochaine image cle
                self._flush(t)

    async def broadcast_state(self):
        msg = json.dumps(self.state())
        for ws in list(self.admins):
            try:
                await ws.send_str(msg)
            except ConnectionError:
                self.admins.discard(ws)


class Remux:
    """Convertit le MP4 fragmente en MPEG-TS (simple copie, quasi sans CPU) pour les TV DLNA."""

    def __init__(self):
        self.proc = None
        self.synced = False

    async def ensure(self):
        if (self.proc and self.proc.returncode is None) or not streamer.init:
            return
        self.proc = await asyncio.create_subprocess_exec(
            imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
            "-fflags", "nobuffer", "-probesize", "32768", "-analyzeduration", "0",
            "-f", "mp4", "-i", "pipe:0", "-c", "copy",
            "-f", "mpegts", "-mpegts_flags", "+resend_headers+pat_pmt_at_frames",
            "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000)
        self.synced = False
        self.proc.stdin.write(streamer.init)
        asyncio.create_task(self._read(self.proc))

    async def _read(self, proc):
        while chunk := await proc.stdout.read(65536):
            hub.push_ts(chunk)
        if proc is self.proc:
            self.proc = None
            hub.close_ts()

    def feed(self, frag: bytes, key: bool):
        p = self.proc
        if not p or p.returncode is not None:
            return
        if not self.synced:
            if not key:
                return
            self.synced = True
        try:
            p.stdin.write(frag)
        except Exception:
            pass

    async def stop(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            p.kill()
        hub.close_ts()


streamer = Streamer()
remux = Remux()
start_lock = asyncio.Lock()
hub = Hub()


@web.middleware
async def local_only(request: web.Request, handler):
    if request.path.startswith(LOCAL_ONLY) and request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden(text="Tableau de bord accessible depuis ce PC uniquement.")
    return await handler(request)


def page(name: str):
    async def h(_):
        return web.FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})
    return h


async def hls_file(request: web.Request):
    path = (STREAM / request.match_info["name"]).resolve()
    if path.parent != STREAM.resolve() or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})


async def live_ts(request: web.Request):
    """Flux MPEG-TS lu directement par les TV (DLNA)."""
    resp = web.StreamResponse(headers={
        "Content-Type": "video/mpeg", "Cache-Control": "no-cache", "transferMode.dlna.org": "Streaming",
        "contentFeatures.dlna.org": "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"})
    await resp.prepare(request)
    if request.method == "HEAD":
        return resp
    for _ in range(100):         # la TV peut demander le flux avant que FFmpeg soit pret
        if streamer.init or not streamer.running:
            break
        await asyncio.sleep(0.1)
    if streamer.running:
        await remux.ensure()
    q: asyncio.Queue = asyncio.Queue(300)
    hub.ts_clients.add(q)
    try:
        while (chunk := await q.get()) is not None:
            await resp.write(chunk)
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        hub.ts_clients.discard(q)
    return resp


async def live_mp4(request: web.Request):
    """Flux MP4 fragmente en direct, lu par les appareils Google Cast."""
    resp = web.StreamResponse(headers={
        "Content-Type": "video/mp4", "Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
    await resp.prepare(request)
    if request.method == "HEAD":
        return resp
    for _ in range(50):          # le flux peut demarrer dans la seconde qui suit
        if streamer.init:
            break
        await asyncio.sleep(0.1)
    c = {"q": asyncio.Queue(300), "synced": False}
    c["q"].put_nowait(streamer.init)
    hub.mp4_clients.append(c)
    try:
        while (chunk := await c["q"].get()) is not None:
            await resp.write(chunk)
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        if c in hub.mp4_clients:
            hub.mp4_clients.remove(c)
    return resp


async def scan_devices():
    hub.scanning = True
    await hub.broadcast_state()
    try:
        results = await asyncio.gather(dlna.discover(), castdev.discover(), return_exceptions=True)
        found = [d for r in results if isinstance(r, list) for d in r]
        old = hub.devices
        hub.devices = {d["id"]: dict(d, casting=old.get(d["id"], {}).get("casting", False)) for d in found}
        for i, d in old.items():          # les appareils ajoutes a la main restent
            if d.get("manual") and i not in hub.devices:
                hub.devices[i] = d
        hub.notice = "" if hub.devices else (
            "Aucun appareil trouvé sur " + ", ".join(dlna.local_ips()) + ". Vérifiez que la box est allumée et sur "
            "le même réseau, que le pare-feu Windows autorise MultiTV (réseau privé), ou ajoutez-la par son adresse IP.")
    finally:
        hub.scanning = False
        await hub.broadcast_state()


async def add_by_ip(ip: str):
    try:
        socket.inet_aton(ip)
    except OSError:
        hub.notice = "Adresse IP invalide."
        return await hub.broadcast_state()
    hub.scanning, hub.notice = True, f"Test de {ip} en cours…"
    await hub.broadcast_state()
    try:
        results = await asyncio.gather(asyncio.wait_for(dlna.probe(ip), 6), asyncio.wait_for(castdev.probe(ip), 8),
                                       return_exceptions=True)
        found = [d for r in results if isinstance(r, list) for d in r]
        for d in found:
            hub.devices[d["id"]] = dict(d, casting=hub.devices.get(d["id"], {}).get("casting", False), manual=True)
        if found:
            hub.notice = ""
        else:
            hub.notice = diag.explain(ip, await diag.services(ip))
    finally:
        hub.scanning = False
        await hub.broadcast_state()


async def cast_to(dev_id: str):
    dev = hub.devices.get(dev_id)
    if not dev:
        return
    dev["error"] = ""
    try:
        async with start_lock:      # plusieurs TV lancees d'un coup : un seul demarrage de FFmpeg
            if not streamer.running or streamer.config["mode"] != "live":
                await streamer.stop(release=False)
                streamer.config["mode"] = "live"
                await streamer.start()
        base = f"http://{local_ip(dev['ip'])}:{PORT}"
        if dev["kind"] == "cast":
            try:        # lecteur temps reel ouvert sur la box (DashCast) ; sinon simple flux video
                await castdev.cast_page(dev, base + "/tv")
                dev["via"] = "temps réel"
            except Exception:
                await castdev.cast(dev, base + "/live.mp4")
                dev["via"] = "flux vidéo (retard 3 à 7 s)"
        else:
            await dlna.cast(dev, base + "/live.ts")
        dev["casting"] = True
    except Exception as e:
        dev["casting"], dev["error"] = False, str(e)[:120] or type(e).__name__
    await hub.broadcast_state()


async def restart_stream():
    if not streamer.running:
        return
    again = [i for i, x in hub.devices.items() if x.get("casting")]
    await streamer.stop(release=False)
    await streamer.start()
    for i in again:           # les TV qui diffusaient reprennent automatiquement
        asyncio.create_task(cast_to(i))


picking = False


async def pick_source():
    """Voile de selection sur le PC ; la diffusion s'adapte aussitot a la nouvelle zone."""
    global picking
    if picking:
        return
    picking = True
    try:
        region = await asyncio.to_thread(capture.pick_region)
        if region:
            streamer.config["source"] = dict(region, kind="region")
            await restart_stream()
    finally:
        picking = False
        await hub.broadcast_state()


async def stop_cast(dev_id: str):
    dev = hub.devices.get(dev_id)
    if dev:
        try:
            await (castdev if dev["kind"] == "cast" else dlna).stop(dev)
        except Exception as e:
            dev["error"] = str(e)[:120]
        dev["casting"] = False
        await hub.broadcast_state()


async def qr(request: web.Request):
    svg = segno.make(request.query.get("data", "")).svg_inline(scale=6, dark="#000", light="#fff")
    return web.Response(text=svg, content_type="image/svg+xml")


async def tv_sender(ws, q: asyncio.Queue):
    """Une file par TV : une TV lente ne ralentit pas les autres."""
    try:
        while True:
            item = await q.get()
            await (ws.send_str(item) if isinstance(item, str) else ws.send_bytes(item))
    except (ConnectionError, RuntimeError):
        pass


async def ws_tv(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    tv_id = None
    async for m in ws:
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        if d.get("type") == "hello":
            tv_id = str(d["id"])[:32]
            old = hub.tvs.get(tv_id)
            if old and old["ws"] and not old["ws"].closed and old["ws"] is not ws:
                await old["ws"].close()
            if old and old.get("task"):
                old["task"].cancel()
            t = hub.tvs[tv_id] = dict(ws=ws, ip=request.remote, ua=d.get("ua", "")[:160],
                                      playing=False, seen=time.time(), q=asyncio.Queue(40), synced=False)
            t["task"] = asyncio.create_task(tv_sender(ws, t["q"]))
            t["q"].put_nowait(hub._mode_msg())
            if streamer.running and streamer.config["mode"] == "live" and streamer.init:
                t["q"].put_nowait(json.dumps({"type": "init", "codec": streamer.codec}))
                t["q"].put_nowait(streamer.init)
        elif d.get("type") == "status" and tv_id in hub.tvs:
            hub.tvs[tv_id]["playing"] = bool(d.get("playing"))
            hub.tvs[tv_id]["seen"] = time.time()
        else:
            continue
        hub.tvs[tv_id]["q"].put_nowait(json.dumps({"type": "name", "name": hub.names.get(tv_id, "")}))
        await hub.broadcast_state()
    if tv_id in hub.tvs and hub.tvs[tv_id]["ws"] is ws:
        hub.tvs[tv_id]["task"].cancel()
        hub.tvs[tv_id].update(ws=None, playing=False)
    await hub.broadcast_state()
    return ws


async def ws_admin(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    hub.admins.add(ws)
    await ws.send_str(json.dumps(hub.state()))
    async for m in ws:
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        t = d.get("type")
        if t == "start":
            await streamer.start()
        elif t == "stop":
            await streamer.stop()
        elif t == "scan" and not hub.scanning:
            asyncio.create_task(scan_devices())
        elif t == "addip":
            asyncio.create_task(add_by_ip(str(d.get("ip", "")).strip()))
        elif t == "cast":
            asyncio.create_task(cast_to(d["id"]))
        elif t == "stopcast":
            asyncio.create_task(stop_cast(d["id"]))
        elif t == "config":
            c = d["config"]
            if c.get("encoder") in ENCODERS:
                streamer.config.update(
                    encoder=c["encoder"],
                    mode=c["mode"] if c.get("mode") in ("live", "hls") else "live",
                    height=max(144, min(2160, int(c["height"]))),
                    fps=max(5, min(60, int(c["fps"]))),
                    bitrate=max(300, min(40000, int(c["bitrate"]))))
                await restart_stream()
        elif t == "monitors":
            await ws.send_str(json.dumps({"type": "monitors", "list": capture.list_monitors(),
                                          "vdd": capture.vdd_installed()}))
        elif t == "installvdd":
            capture.install_vdd()
        elif t == "extend":
            capture.extend_displays()
        elif t == "windows":
            await ws.send_str(json.dumps({"type": "windows", "list": capture.list_windows()}))
        elif t == "pick":
            asyncio.create_task(pick_source())
        elif t == "source":
            kind = d.get("kind")
            if kind == "desktop":
                streamer.config["source"] = {"kind": "desktop"}
            elif kind == "monitor" and d.get("device"):
                streamer.config["source"] = {"kind": "monitor", "device": str(d["device"]),
                                             "name": str(d.get("name", "")), "x": 0, "y": 0, "w": 0, "h": 0}
            elif kind == "window" and d.get("title"):
                streamer.config["source"] = {"kind": "window", "title": str(d["title"])}
            else:
                continue
            await restart_stream()
        elif t == "tv":
            tv = hub.tvs.get(d["id"])
            if d["cmd"] == "rename":
                hub.names[d["id"]] = str(d["name"])[:40]
                hub.save()
                if tv and tv["ws"]:
                    tv["q"].put_nowait(json.dumps({"type": "name", "name": hub.names[d["id"]]}))
            elif d["cmd"] == "forget":
                hub.tvs.pop(d["id"], None)
                hub.names.pop(d["id"], None)
                hub.save()
            elif tv and tv["ws"]:
                tv["q"].put_nowait(json.dumps({"type": "cmd", "cmd": d["cmd"]}))
        await hub.broadcast_state()
    hub.admins.discard(ws)
    return ws


async def on_cleanup(_):
    await streamer.stop()


def main():
    app = web.Application(middlewares=[local_only])
    app.add_routes([
        web.get("/", page("tv.html")), web.get("/tv", page("tv.html")),
        web.get("/admin", page("dashboard.html")),
        web.get("/qr.svg", qr),
        web.get("/ws/tv", ws_tv), web.get("/ws/admin", ws_admin),
        web.get("/hls/{name}", hls_file), web.get("/live.ts", live_ts), web.get("/live.mp4", live_mp4),
        web.static("/static", STATIC),
    ])
    app.on_cleanup.append(on_cleanup)
    if FROZEN:   # version .exe : ouvre directement le tableau de bord
        async def open_browser(_):
            webbrowser.open(f"http://localhost:{PORT}/admin")
        app.on_startup.append(open_browser)
    print(f"\n  MultiTV - tableau de bord : http://localhost:{PORT}/admin\n")
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("MultiTV utilise gdigrab (Windows uniquement).")
    main()
