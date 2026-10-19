"""Diagnostic d'une box : quels services reseau repondent a cette adresse IP ?"""
import asyncio

PORTS = {
    8009: "Google Cast", 8008: "Google Cast (HTTP)", 5555: "Android (ADB)",
    7000: "AirPlay", 8060: "Roku", 3000: "LG webOS", 3001: "LG webOS",
    8001: "Samsung Tizen", 8002: "Samsung Tizen", 55000: "Samsung (ancien)",
    9000: "DLNA/UPnP?", 8200: "DLNA (MiniDLNA)", 2869: "UPnP", 7676: "UPnP", 5000: "UPnP",
    49152: "UPnP", 49153: "UPnP", 49154: "UPnP", 49155: "UPnP", 49156: "UPnP", 49157: "UPnP",
    80: "Interface web", 8080: "Interface web", 554: "RTSP (flux IPTV)",
}


async def _open(ip: str, port: int) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 0.8)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def services(ip: str) -> dict[int, str]:
    res = await asyncio.gather(*(_open(ip, p) for p in PORTS))
    return {p: PORTS[p] for p, ok in zip(PORTS, res) if ok}


def explain(ip: str, open_ports: dict[int, str]) -> str:
    names = sorted(set(open_ports.values()))
    if not open_ports:
        return (f"Aucun service de diffusion ne répond sur {ip} : box éteinte ou en veille, pare-feu de la box, "
                f"ou box sans DLNA ni Google Cast.")
    base = f"{ip} : services ouverts = {', '.join(names)}. "
    if any("Cast" in n for n in names):
        return base + "Google Cast présent mais la connexion a échoué : redémarrez la box et réessayez."
    if any("UPnP" in n or "DLNA" in n for n in names):
        return base + ("Service DLNA/UPnP présent mais il ne répond pas à l'adresse directe : utilisez « Rechercher » "
                       "et vérifiez que le pare-feu Windows autorise MultiTV (réseau privé).")
    if any(k in n for n in names for k in ("AirPlay", "Roku", "LG", "Samsung")):
        return base + "Protocole propriétaire non géré pour l'instant (AirPlay, Roku, LG ou Samsung)."
    return base + ("Aucun récepteur DLNA ni Google Cast : cette box ne sait pas recevoir de diffusion. "
                   "Installez une appli de réception (ex. BubbleUPnP) ou utilisez le navigateur de la box.")
