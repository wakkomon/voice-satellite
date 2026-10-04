#!/usr/bin/env python3
"""Mergt TEMPLATE-Key/Value-Paare in eine bestehende .env, ohne vorhandene
Werte zu ueberschreiben (Secrets bleiben unangetastet).

    python3 scripts/sync_env.py [.env-Pfad] [--set KEY=VALUE]...

Ohne --set dient .env.example neben dem Repo-Root als Template.
Vorhandene Keys werden nur ergaenzt, nie ersetzt; neue Keys mit leerem Wert
kommen nur dazu, wenn sie im Template einen Wert haben.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def parse(path):
    """-> (geordnete Liste (key, raw_line), dict key->value)"""
    lines, kv = [], {}
    if not os.path.exists(path):
        return lines, kv
    with open(path, encoding="utf-8") as f:
        for raw in f:
            m = re.match(r"^([A-Z0-9_]+)\s*=\s*(.*)$", raw.strip())
            if m and not raw.strip().startswith("#"):
                kv[m.group(1)] = m.group(2).strip("\"'")
            lines.append((m.group(1) if m else None, raw.rstrip("\n")))
    return lines, kv


def main(argv):
    env_path = argv[0] if argv and not argv[0].startswith("--") else os.path.join(ROOT, ".env")
    pairs = []
    for a in argv:
        if a.startswith("--set="):
            pairs.append(a[6:])
    args = iter(argv)
    for a in argv:
        if a == "--set":
            for b in args:
                if b.startswith("--"):
                    break
                pairs.append(b)

    if not pairs:  # Template-Modus
        tpl = os.path.join(ROOT, ".env.example")
        for line in open(tpl, encoding="utf-8"):
            m = re.match(r"^([A-Z0-9_]+)=(.+)$", line.strip())
            if m and m.group(2).strip():
                pairs.append(m.group(1) + "=" + m.group(2).strip())

    lines, kv = parse(env_path)
    added, present = [], []
    for p in pairs:
        k, _, v = p.partition("=")
        k = k.strip()
        if k in kv:
            present.append(k)
        else:
            added.append((k, v))

    if not added:
        print(f"{env_path}: alle {len(present)} Template-Keys vorhanden, nichts geaendert.")
        return 0

    # neue Keys anhaengen, erhaltenes File bleibt unveraendert
    body = "".join(l + "\n" for _, l in lines)
    if body and not body.endswith("\n"):
        body += "\n"
    body += "\n# ── ergaenzt von sync_env.py ─────────────────────────────\n"
    for k, v in added:
        body += f"{k}={v}\n"
    tmp = env_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
    print(f"{env_path}: {len(added)} Keys ergaenzt ({', '.join(k for k, _ in added)}); "
          f"{len(present)} vorhanden, nicht ueberschrieben.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
