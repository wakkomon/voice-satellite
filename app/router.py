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
import threading

from . import config, http, memory, playback

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


def _normalize_both(s):
    """(mit Leerzeichen, ohne Leerzeichen) – 'flur lampe' matcht so auch 'flurlampe'."""
    n = _normalize(s)
    return n, re.sub(r"\s+", "", n)


def _match_device(text):
    """Findet Gerät per Label/Metadaten-Alias im gesprochenen Satz."""
    t, tc = _normalize_both(text)
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
            n, nc = _normalize_both(lbl)
            if n and (n in t or nc in tc):
                if best is None or len(nc) > len(_normalize_both(best[0])[1]):
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


def _is_state_question(t):
    """Satz enthaelt BOTH ON- und OFF-Wort (z.B. 'licht an oder aus?') ->
    das ist eine Frage, KEIN Schalten. Rueckgabe True = ans LLM
    weitergeben, nichts schalten."""
    on = any(re.search(rf"\b{w}\b", t) for w in DEFAULT_SYNONYMS["on"])
    off = any(re.search(rf"\b{w}\b", t) for w in DEFAULT_SYNONYMS["off"])
    return on and off


def try_openhab(text):
    """-> Antwortstring wenn Befehl an openHAB ging, sonst None (-> LLM)."""
    match = _match_device(text)
    if not match:
        return None
    label, item = match
    t = _normalize(text)
    if _is_state_question(t):
        # '... an oder aus?' – beides vorgehalten = Frage, kein Befehl.
        # Nichts schalten, an das LLM weitergeben.
        log.info("Zustandsfrage erkannt – ans LLM: %r", text[:60])
        return None
    value = _value_for(text)
    if value is None:
        # Gerät erkannt aber kein Zustand -> Status abfragen
        try:
            st = http.get_text(f"{config.OPENHAB_URL}/{item['name']}/state",
                               token=config.OPENHAB_TOKEN, timeout=10)
            st = {"ON": "an", "OFF": "aus"}.get(st, st)
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


def ask_llm(text, channel="voice"):
    """LLM-Fallback mit Kurz- + Langzeitgedächtnis.

    channel: Kurzhistorie NUR dieses Kanals im Prompt – Webui-Tippsachen
    contaminieren keine Sprachkonversation (und umgekehrt).

    Werkzeug-Modus (Hermes-Gateway): anderer Systemprompt, der dem Agenten
    erlaubt – und befoehlt! – Tatsachen per Kommando zu pruefen statt zu
    raten. Reines Chat-Modell: altbewaehrter 'keine Werkzeuge'-Prompt."""
    tools = config.llm_has_tools()
    system = config.AGENT_SYSTEM_PROMPT if tools else config.SYSTEM_PROMPT
    facts = memory.load_facts()
    if facts:
        system += "\n\nDas weißt du bereits über den Nutzer:\n- " + "\n- ".join(facts[:30])
    messages = ([{"role": "system", "content": system}]
                + memory.recent_history(channel)
                + [{"role": "user", "content": text}])
    payload = {"model": config.LLM_MODEL, "messages": messages, "stream": False,
               "max_tokens": config.LLM_MAX_TOKENS}
    if not tools:
        # vLLM/Ollama-Felder – ein Agent-Endpunkt (Gateway) erwartet nur
        # Standard-OpenAI-Felder.
        # Ollama: Modell 30 min resident halten – naechster Befehl
        # antwortet in ~1s statt 30s Kaltstart
        payload["keep_alive"] = "30m"
        # Qwen3.8-27B (vLLM): Thinking aus = 2-4s statt ~26s. Der Antworttext
        # landet dann in reasoning_content statt content -> beide abfragen.
        if not config.LLM_ENABLE_THINKING:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

    def _extract(d):
        m = d["choices"][0]["message"]
        return (m.get("content") or m.get("reasoning_content") or "").strip()

    try:
        data = http.post_json(config.LLM_URL, payload, token=config.LLM_TOKEN,
                              timeout=config.LLM_TIMEOUT, retries=0)
        out = remove_emojis(_extract(data))
        if not out:
            log.warning("LLM lieferte leeren Inhalt (Thinking-Modell?); 2. Versuch")
            data = http.post_json(config.LLM_URL, payload, token=config.LLM_TOKEN,
                                  timeout=config.LLM_TIMEOUT, retries=0)
            out = remove_emojis(_extract(data))
        if not out:
            log.warning("LLM lieferte trotz 2. Versuchs leeren Inhalt")
            return "Das habe ich leider gerade nicht verstanden.", False
        return out, True
    except Exception as e:
        log.error("LLM-Fehler: %s", e)
        return "Das LLM ist gerade nicht erreichbar.", False


def remove_emojis(text):
    # Markdown-Signale entfernen (27B formatiert trotz Anweisung): **fett**, *kursiv*,
    # Listenstriche, Tabellen – sonst liest Piper Sternchen aus.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)
    text = re.sub(r"\|", " ", text)
    return re.sub(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF\u2702-\u27B0\u2B00-\u2BFF\uFE0F\u2764\u2190-\u21FF\u2600-\u26FF]",
        "", text).strip()


def warm_async():
    """Modell nach dem Start laden, damit die erste Sprachfrage nicht im
    Kaltstart (~35s) haengt. Feuert ein minimales Chat-Completion."""
    def _w():
        try:
            http.post_json(config.LLM_URL, {
                "model": config.LLM_MODEL,
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 4, "stream": False, "keep_alive": "30m"},
                token=config.LLM_TOKEN, timeout=config.LLM_TIMEOUT, retries=0)
            log.info("LLM warm: %s", config.LLM_MODEL)
        except Exception as e:
            log.warning("LLM-Warmup fehlgeschlagen: %s", e)
    threading.Thread(target=_w, daemon=True).start()


def try_music_stop(text):
    """'Stopp', 'Musik aus', 'Radio aus' ... -> Wiedergabe direkt beenden,
    ohne LLM-Runde (schnell + deterministisch). None, wenn es kein
    Stop-Kommando ist."""
    t = _normalize(text)
    stop_words = ("stopp", "stop", "pause", "pausiere", "genug",
                  "ausreichend", "lass gut", "lass es gut")
    has_stop = any(re.search(rf"\b{w}\b", t) for w in stop_words)
    if not has_stop:
        return None
    # '... aus' gilt nur fuer Musik/Radio, nicht fuer Lampen:
    if not any(w in t for w in ("musik", "radio", "wiedergabe",
                                "spielen", "laeuft", "song")):
        if not re.fullmatch(r"(bitte\s+)?stopp(.*?)\?", t):
            return None
    stopped = playback.player.stop()
    if stopped:
        return "Musik gestoppt."
    return None  # nichts lief -> evtl. doch LLM (z.B. 'Stopp, sag mal ...')


def handle(text, channel="voice"):
    """Haupt-Route: erst Musik-Stop, dann openHAB, sonst LLM.

    channel 'voice'|'web' – steuert, in welchen Kurzzeitgedächtnis-Zweig
    der Austausch geschrieben wird und aus welchem er Kontext liest.

    Gibt (antwort, 'smarthome'|'llm', llm_ok) zurück; llm_ok=False bei
    LLM-Ausfall/leerer Antwort (für den Fehlerton)."""
    ms = try_music_stop(text)
    if ms is not None:
        memory.add_exchange(text, ms, channel)
        return ms, "smarthome", True
    oh = try_openhab(text)
    if oh is not None:
        memory.add_exchange(text, oh, channel)
        return oh, "smarthome", True
    ans, ok = ask_llm(text, channel)
    memory.add_exchange(text, ans, channel)
    memory.extract_facts_async(text, ans)
    return ans, "llm", ok
