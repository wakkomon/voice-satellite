# Connman + Docker-Build: veth-Default-Route-Problem

## Symptom

Auf dem **2. Pi (display02, 192.168.1.26)** schlug der Docker-Build
(`apt-get install` / `pip install`) wiederholt mit
`Unable to connect to ... [IP: ... 80]: No route to host` fehl –
während WLAN (`wlan0`, Metrik 100) und der Host selbst problemlos im
Internet waren.

## Ursache (Root Cause)

`connman` auf dem Pi behandelt jedes neue Docker-`veth`-Interface als
neues Netzwerk:

1. `portainer_agent` (oder jeder andere Docker-Container) erzeugt
   `vethXXXX` auf dem Host.
2. `connman` konfiguriert es, bekommt eine IP und setzt eine
   **Default-Route über das veth mit Metrik 0** – die schlägt die
   WLAN-Route (Metrik 100) komplett.
3. Der Docker-Build-Container ruft das Internet ab → Traffic geht über
   das veth → Portainer-Agent-Container → keine Route zurück →
   `No route to host`.
4. Sobald der veth-Docker-Container gestoppt/restartet wird, bricht
   der Default-Route-Sumpf weiter um (Route kommt kurz weg, kommt
   wieder).

### Warum die Blacklist nicht gegriffen hat

`/etc/connman/main.conf` hatte:

```ini
# NetworkInterfaceBlacklist = vmnet,vboxnet,virbr,ifb,ve-,vb-
```

Zwei Fehler:
- Die Zeile war **kommentiert** (`#`).
- Selbst aktiviert wäre `ve-` **zu kurz/kurz falsch** – Docker-Interfaces
  heißen `vethXXXXXXXX` (keine Bindestrich-Präfix wie `ve-`).

## Fix (auf display02 angewendet)

```bash
# Backup
sudo cp /etc/connman/main.conf /etc/connman/main.conf.bak

# Blacklist aktivieren + veth (Docker) + vbox (virtuelle Netzwerke) ergänzen
sudo sed -i \
  "s/# NetworkInterfaceBlacklist = vmnet,vboxnet,virbr,ifb,ve-,vb-//" \
  /etc/connman/main.conf
# und sicherstellen, dass am Ende steht:
# NetworkInterfaceBlacklist = vmnet,vboxnet,virbr,ifb,veth

sudo systemctl restart connman
```

Danach:

```bash
ip route        # keine Default via veth mehr
docker run --rm debian:bookworm sh -c 'apt-get update -qq'  # OK
```

**Wichtig:** Nach dem `connman`-Restart trennt das WLAN kurz (~1–2 min)
und verbindet sich automatisch wieder. SSH über WLAN bricht in dieser
Zeit ab – nicht stören, dann neu verbinden.

## Zusätzlich in deploy.sh (Schutz für andere Hosts)

`deploy.sh` stoppt `portainer_agent` vor dem Build (Section `0b`) und
startet es danach wieder (Section `0c`). Das reduziert die
veth-Kollision, ist aber **kein Ersatz** für die Connman-Blacklist:
Auch ohne Portainer erzeugt jeder `docker run`-Container ein veth und
Connman würde es adoptieren.

Die retry-Loops im `Dockerfile` (apt-get: 3×, 15 s; pip: 3×, 15 s)
decken Kurzunterbrechungen ab.

## Prüf-Befehle

```bash
# veth-Interfaces existieren?
ip -br link | grep veth

# Default-Route über veth?
ip route | grep default

# Connman-Blacklist aktiv?
grep -E '^NetworkInterfaceBlacklist' /etc/connman/main.conf

# Build-Netzwerk testen (ohne den eigentlichen Build)
sudo docker run --rm debian:bookworm sh -c \
  'apt-get update -qq && echo NET_OK || echo NET_FAIL'
```

## Erwartetes Verhalten nach Fix

| Host | connman | veth-Blacklist | Build |
|---|---|---|---|
| display01 (.32) | nein | – | OK |
| display02 (.26) | ja | ja (`veth`) | OK (dank Blacklist + Retry) |

## Historie

- `765497d` – portainer_agent vor Build stoppen (1. Versuch, nicht ausreichend)
- `162799b` – apt-get retry-Loop (2. Versuch)
- `329793d` – pip install retry-Loop (3. Versuch)
- `ac19ddb` – Offline-Deploy-Modus `VS_IMAGE_TAR` (4. Versuch)
- Connman-Blacklist `veth` + Restart (5. Versuch, **Root-Cause-Fix**)
- `90ce87a` – `docker stop` + `rm` vor Container-Start (env-aktualisierung)
