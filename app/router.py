"""Router: Text -> openHAB-Befehl (konfigurierbare Rules) oder LLM-Fallback.

Regel-Datei (YAML) ersetzt die hartkodierten Regexe aus openhab_switch.py:
  - label: "licht kitchen"        # exakter openHAB-Label (case-insensitive)
    aliases: ["küchenlicht"]
    actions: {on: "Licht an.", off: "Licht aus."}
Matcht KEIN openHAB-Gerät -> LLM (Hermes .33) mit Gedächtnis-Kontext.
Item-Cache (5 min) spart REST-Last und Latenz.
"""
import logging
import re

from . import config, http, memory

log = logging.getLogger("router")

# Fallback-Konjunktionen, wenn keine YAML-Rules existieren
DEFAULT_SYNONYMS = {
    "on": ["an", "ein", "einschalten", "angehen"],
    "off": ["aus", "ausschalten", "ausgehen", "schliessen", "schließen"],
}

_NUM_RE = re.compile(
    r"(?:(?:schalte|setze)\s+)?(?:das|die|der|den|ein|eine)?\s*(.+?)\s+"
    r"(?:jetzt|bitte|sofort|wieder)?\s*(?:auf\s+(\d+)\s*(?:grad|prozent|%|°)?|(an|ein|aus))",
    re.IGNORECASE,
)


def load_rules():
    """Optional: YAML/Rules-Datei; ohne PyYAML-Abhängigkeit minimal geparst."""
    try:
        import yaml
        with open(config.RULES_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("rules", [])
    except ImportError:
        log.warning("PyYAML fehlt – nutze openHAB-Label-Matching ohne Regeln")
        return []
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("Rules-Datei fehlerhaft: %s", e)
        return []


class ItemCache:
    def __init__(self):
        self._items = None
        self._ts = 0

    def get(self):
        import time
        now = time.time()
        if self._items is None or now - self._ts > config.CACHE_TTL:
            try:
                data = http.get_json(f"{config.OPENHAB_URL}?recursive=false",
                                     token=config.OPENHAB_TOKEN, timeout=15)
                self._items = data
                self._ts = now
            except Exception as e:
                log.error("openHAB nicht erreichbar: %s", e)
                if self._items is None:
                    return []
        return self._items or []


_cache = ItemCache()


def _normalize(s):
    return re.sub(r"\s+", " ", s.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")).strip()


def _match_device(text):
    """Findet Gerät per Label/Metadaten-Alias im gesprochenen Satz."""
    t = _normalize(text)
    items = _cache.get()
    best = None
    for item in items:
        labels = [item.get("label") or item.get("name") or ""]
        meta = item.get("metadata") or {}
        va = (meta.get("VoiceAlias") or {}).get("value") or ""
        for a in va.split(","):
            if a.strip():
                labels.append(a)
        for lbl in labels:
            n = _normalize(lbl)
            if n and n in t:
                if best is None or len(n) > len(_normalize(best[0])):
                    best = (lbl, item)
    return best  # (label, item) oder None


def _value_for(text):
    """ON/OFF/Prozent/Grad aus dem Satz ziehen (wie legacy openhab_switch)."""
    t = _normalize(text)
    for w in DEFAULT_SYNONYMS["off"]:
        if re.search(rf"\b{w}\b", t):
            return "OFF"
    for w in DEFAULT_SYNONYMS["on"]:
        if re.search(rf"\b{w}\b", t):
            return "ON"
    m = re.search(r"(\d+)\s*(?:prozent|%)", t)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)\s*(?:grad|°)", t)
    if m:
        return m.group(1)
    return None


def try_openhab(text):
    """-> Antwortstring wenn Befehl an openHAB ging, sonst None (-> LLM)."""
    match = _match_device(text)
    if not match:
        return None
    label, item = match
    value = _value_for(text)
    if value is None:
        # Gerät erkannt aber kein Zustand -> Status abfragen
        try:
            st = http.get_json(f"{config.OPENHAB_URL}/{item['name']}/state",
                               token=config.OPENHAB_TOKEN, timeout=10)
            return f"{label} ist {st}."
        except Exception as e:
            return f"{label}: Status nicht abrufbar ({e})."
    try:
        ok = http.post_raw(f"{config.OPENHAB_URL}/{item['name']}", value,
                           token=config.OPENHAB_TOKEN, timeout=10)
        if ok:
            if value in ("ON", "OFF"):
                return f"{label} ist {'an' if value == 'ON' else 'aus'}."
            unit = " Prozent" if re.search(r"prozent|%", _normalize(text)) else ""
            return f"{label} auf {value}{unit}."
        return f"{label}: Kommando fehlgeschlagen."
    except Exception as e:
        log.error("openHAB-Befehl fehlgeschlagen: %s", e)
        return None  # dann doch LLM fragen


def ask_llm(text):
    """LLM-Fallback mit Kurz- + Langzeitgedächtnis."""
    system = config.SYSTEM_PROMPT
    facts = memory.load_facts()
    if facts:
        system += "\n\nDas weißt du bereits über den Nutzer:\n- " + "\n- ".join(facts[:30])
    messages = [{"role": "system", "content": system}] + memory.recent_history() + [{"role": "user", "content": text}]
    payload = {"model": config.LLM_MODEL, "messages": messages, "stream": False,
               "max_tokens": 400}
    try:
        data = http.post_json(config.LLM_URL, payload, token=config.LLM_TOKEN,
                              timeout=config.LLM_TIMEOUT, retries=0)
        out = data["choices"][0]["message"]["content"].strip()
        return remove_emojis(out) or "Keine Antwort erhalten."
    except Exception as e:
        log.error("LLM-Fehler: %s", e)
        return "Das LLM ist gerade nicht erreichbar."


def remove_emojis(text):
    return re.sub(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF\u2702-\u27B0\u2B00-\u2BFF\uFE0F\u2764\u2190-\u21FF\u2600-\u26FF]",
        "", text).strip()


def handle(text):
    """Haupt-Route: erst openHAB, sonst LLM. Gibt (antwort, 'smarthome'|'llm') zurück."""
    oh = try_openhab(text)
    if oh is not None:
        memory.add_exchange(text, oh)
        return oh, "smarthome"
    ans = ask_llm(text)
    memory.add_exchange(text, ans)
    memory.extract_facts_async(text, ans)
    return ans, "llm"
