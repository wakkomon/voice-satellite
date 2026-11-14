# FEATURE_STATUS — Regelbasierte Funktionen ohne LLM

Stand: Lauf ENDE (erledigt); Belege in workspace evidence/ (pit_1..3.txt, app_build_rules.log).

## Pi-Repo: /home/agent/.hermes/projects/pidisplay-voice/voice-satellite
- app/ruleintents.py (Parser+Executor, 927 Z.), app/schedule.py, master/schedule.py, master/api.py (Routen + CAMERAS im Config-Pull), master/satconfig.py (CAMERAS-Whitelist), app/router.py (Hook vor openHAB/LLM), app/intent.py (try_show_camera_config, urllib-Fix), app/web.py (/notify, /tone signal), app/tones.py (signal 10 s), app/display.py (show_image), app/config.py (CAMERAS), app/main.py (standalone-Ticker).
- Tests GRUEN: tests/test_ruleintents.py (88 ok), tests/test_schedule.py (alle), tests/test_smoke_ruleflow.py (end-to-end standalone+master-Mock). Beleg: evidence/pit_1..3.txt.
- Bestand: tests/test_sat_disable.py + test_master_token_auth.py schlagen AUCH im Baseline-Stand fehl (vor meiner Aenderung, git-Stash-Vergleich) — keine Regression, Umgebung (braucht live-Master-Kontext).

## App-Repo: /home/agent/.hermes/projects/voice-satellite-app
- NEU core/RuleIntents.kt (483 Z., Parser-Port 1:1), core/RuleExec.kt (408 Z.: SatCtx-Port + RuleStore + 1-s-Ticker).
- GEPAECHT: core/VoiceFlow.kt (Hook answerFromWav + runTextPipeline, playSignal, ensureTicker, onShowImage-Callback), core/MasterClient.kt (kind/tone_seconds + cameras aus Heartbeat-Config), core/AppSettings.kt (cameras), MainViewModel.kt (Signalton-Zweig, Kamera-Pull, onShowImage->Push-Image-Overlay).
- NICHT angefasst: build.gradle.kts, Manifest, MainActivity-OTA-Teil (Parallelauftrag).
- Build: dev-Checkout-KOPI /home/hermes/voice-satellite-app-rules (beruehrte OTA-Arbeitskopie NICHT ueberschrieben): `gradle :app:assembleDebug` = BUILD SUCCESSFUL in 3m 29s (19,5 MB APK). Beleg: evidence/app_build_rules.log.

## openwakeword (Punkt 9) — Ergebnis: NICHT drop-in, begründeter Plan statt Implementierung
- Lizenz: Code Apache 2.0; PRE-TRAINED MODELS CC BY-NC-SA 4.0 (Nicht-Kommerziell+SA) — Privatgebrauch ok, aber keine Verteilung in der APK ohne Eigenmodell; Backbone (Google Speech Embeddings) Apache-2.0.
- Pi 3 (.32, armv7l 32-bit, Py3.11): BLOCKIERT — openwakeword 0.6.0 braucht onnxruntime (kein armv7l-Wheel auf PyPI) + tflite-runtime (armv7l-Wheel nur bis cp39, Pi hat 3.11). Moeglich erst nach 64-bit-Migration (onnxruntime/tflite aarch64-Wheels vorhanden;laut README: 1 Pi-3-Kern laeuft 15-20 OWW-Modelle real-time) oder eigenem NumPy-Port (mehrere Tage, Abweichung upstream).
- App: prinzipiell moeglich — TFLite-Android-Runtime (armv7a+aarch64) laedt die .tflite-Modelle; Mel-Spektrogramm-Praprocessing muesste nach Kotlin portiert werden (~1-2 KB-Code), OWW-Engine hinter WakeWordEngine als optionaler, per Config waehlbarer Pfad (WAKE_ENGINE=porcupine|openwakeword) — Porcupine-Pfad bleibt Standard, Risiko 0.
- Empfohlener naechster Schritt: (1) App-OwwEngine (TFLite + Mel-Port) als separater Auftrag mit Geräte-Test; (2) Pi nach 64-bit-Migration via pip; (3) für beide: eigenes Wake-Word-Modell trainieren (Colab, <1 h), um NC-Lizenz zu umgehen.