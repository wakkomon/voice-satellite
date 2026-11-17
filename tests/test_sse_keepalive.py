"""SSE-Streams (/logs, /events) müssen Keepalive-Pings senden.

Grund: ueber den oeffentlichen Pfad (ai.juerging.net, Hairpin/NAT) werden
idle TCP-Verbindungen nach ~30 s abgerissen (NS_ERROR_NET_PARTIAL/INTERRUPT,
'Verbindung unterbrochen' alle ~30-60 s im WebUI). Static-Check, da api.py
lokal nicht importierbar ist (Starlette fehlt)."""
import pathlib
src = (pathlib.Path(__file__).resolve().parents[1] / "master/api.py").read_text(encoding="utf-8")

def _fn(name):
    i = src.index(f"async def {name}")
    j = src.index("StreamingResponse(generator()", i)
    return src[i:j]

for fn in ("_events", "_live_log"):
    body = _fn(fn)
    assert "asyncio.wait_for(q.get(), timeout=10)" in body, fn
    assert 'yield ": ping\\n\\n"' in body, fn
    print("PASS", fn)
print("ALL OK")
