# Descarga los badges globales de Kick a la carpeta "badges" (el servidor también lo hace solo).
import urllib.request
from pathlib import Path

BASE = "https://cpwemotes.co.uk/kick/kickBadges/"
NAMES = ["broadcaster", "moderator", "vip", "og", "founder", "verified",
         "staff", "subGifter", "sidekick", "subscriber"]
out = Path(__file__).parent / "badges"
out.mkdir(exist_ok=True)
for n in NAMES:
    try:
        req = urllib.request.Request(BASE + n + ".svg", headers={"User-Agent": "Mozilla/5.0"})
        (out / f"{n}.svg").write_bytes(urllib.request.urlopen(req, timeout=15).read())
        print("ok     ", n)
    except Exception as e:
        print("falló  ", n, e)
