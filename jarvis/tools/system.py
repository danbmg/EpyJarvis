"""État du PC : santé, consommation, mises à jour. Tout est en lecture seule."""

from __future__ import annotations

import datetime
import subprocess
import time
from collections import defaultdict

import psutil

from jarvis.tools import Param, Result, Tool, Toolbox
from jarvis.tools.shell import _run as run_powershell_code


def _top(values: dict[str, float], unit: str, n: int = 6) -> str:
    items = sorted(values.items(), key=lambda kv: kv[1], reverse=True)[:n]
    return ", ".join("{} {:.0f}{}".format(name, value, unit) for name, value in items)


def system_health() -> Result:
    procs = list(psutil.process_iter(["name", "memory_info"]))
    for p in procs:  # première mesure CPU, la seconde après 1 s
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    total_cpu = psutil.cpu_percent(interval=1.0)
    cpu, ram = defaultdict(float), defaultdict(float)
    for p in procs:
        try:
            name = (p.info["name"] or "?").removesuffix(".exe")
            if name in ("System Idle Process", "Idle"):
                continue
            cpu[name] += p.cpu_percent(None) / psutil.cpu_count()
            ram[name] += p.info["memory_info"].rss / 2**20 if p.info["memory_info"] else 0
        except psutil.Error:
            continue

    mem = psutil.virtual_memory()
    lines = [
        "Processeur : {:.0f} % utilisé ({} cœurs logiques).".format(total_cpu, psutil.cpu_count()),
        "Mémoire : {:.1f} / {:.1f} Go utilisés ({:.0f} %).".format(
            mem.used / 2**30, mem.total / 2**30, mem.percent),
        "Plus gros consommateurs de processeur : " + _top(cpu, " %"),
        "Plus gros consommateurs de mémoire : " + _top(ram, " Mo"),
    ]
    for part in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        lines.append("Disque {} : {:.0f} Go libres sur {:.0f} Go ({:.0f} % utilisé).".format(
            part.mountpoint, usage.free / 2**30, usage.total / 2**30, usage.percent))
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        if gpu:
            name, util, used, total, temp = [x.strip() for x in gpu.splitlines()[0].split(",")]
            lines.append("Carte graphique {} : {} % utilisée, {} / {} Mo de mémoire, {} °C.".format(
                name, util, used, total, temp))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    disks = run_powershell_code("Get-PhysicalDisk | ForEach-Object { \"$($_.FriendlyName) : $($_.HealthStatus)\" }",
                                timeout=15)
    if disks:
        lines.append("Santé des disques : " + "; ".join(disks.splitlines()))
    uptime = datetime.timedelta(seconds=int(time.time() - psutil.boot_time()))
    lines.append("Allumé depuis {} jours et {} heures.".format(uptime.days, uptime.seconds // 3600))
    battery = psutil.sensors_battery()
    if battery:
        lines.append("Batterie : {:.0f} %{}.".format(battery.percent, " (en charge)" if battery.power_plugged else ""))
    return Result(True, "\n".join(lines))


_WINDOWS_UPDATE = """
$searcher = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher()
$result = $searcher.Search("IsInstalled=0 and IsHidden=0")
if ($result.Updates.Count -eq 0) { "Aucune mise à jour Windows en attente." }
else { $result.Updates | ForEach-Object { "- " + $_.Title } }
"""


def check_updates() -> Result:
    lines = ["Mises à jour Windows :"]
    try:
        lines.append(run_powershell_code(_WINDOWS_UPDATE, timeout=180) or "(pas de réponse)")
    except subprocess.TimeoutExpired:
        lines.append("Windows Update n'a pas répondu à temps.")
    lines.append("\nLogiciels avec une mise à jour disponible (winget) :")
    try:
        out = subprocess.run(["winget", "upgrade", "--accept-source-agreements", "--disable-interactivity"],
                             capture_output=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        text = out.stdout.decode("utf-8", errors="replace")
        rows = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith(("-", "\\", "|", "/"))]
        lines.append("\n".join(rows[-40:]) if rows else "Aucune.")
    except (OSError, subprocess.TimeoutExpired):
        lines.append("winget indisponible.")
    return Result(True, "\n".join(lines))


def register(box: Toolbox, sleep, quit_app) -> None:
    box.add(Tool("system_health",
                 "Mesure l'état de santé du PC : processeur, mémoire, disques, carte graphique, et les "
                 "programmes qui consomment le plus.", system_health, reads=True))
    box.add(Tool("check_updates",
                 "Vérifie les mises à jour en attente : Windows Update (pilotes compris) et logiciels "
                 "(winget). Ne les installe pas.",
                 check_updates, reads=True,
                 notice="Je vérifie les mises à jour, ça peut prendre une minute."))
    box.add(Tool("jarvis_sleep",
                 "Met Jarvis en veille : il libère la carte graphique et n'écoute plus que « Ok Jarvis ». "
                 "Pour « mets-toi en veille », « au revoir », « bonne nuit », « à plus tard ».",
                 sleep))
    box.add(Tool("jarvis_quit",
                 "Ferme complètement Jarvis. Uniquement si l'utilisateur demande explicitement de quitter "
                 "ou fermer Jarvis lui-même.", quit_app))
