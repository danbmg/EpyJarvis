"""PowerShell en accès libre contrôlé.

Jarvis écrit lui-même ses scripts. Avant exécution, le script est analysé par l'analyseur
syntaxique de PowerShell (pas par une recherche de mots) :
  - lecture seule (Get-*, Measure-*, ...)          -> exécuté directement
  - modification (lancer, copier, arrêter un processus...) -> confirmation de l'utilisateur
  - interdits (supprimer, éteindre, paramètres, mots de passe, installer,
    code dynamique qui empêcherait l'analyse)        -> refusé
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

from jarvis.tools import ALLOW, Check, Param, Result, Tool, Toolbox, Verdict, confirm, forbid

log = logging.getLogger("jarvis.tools.shell")

_TMP = Path(tempfile.gettempdir()) / "jarvis"

_ANALYZER = r"""
$code = Get-Content -LiteralPath '{path}' -Raw -Encoding UTF8
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($code, [ref]$tokens, [ref]$errors)
$commands = @(foreach ($c in $ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.CommandAst] }}, $true)) {{
    $name = $c.GetCommandName()
    if ($name) {{
        $a = Get-Alias -Name ([System.Management.Automation.WildcardPattern]::Escape($name)) -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($a) {{ $name = $a.ResolvedCommandName }}
    }}
    [pscustomobject]@{{ name = $name; text = $c.Extent.Text; params = @($c.CommandElements | Where-Object {{ $_ -is [System.Management.Automation.Language.CommandParameterAst] }} | ForEach-Object {{ $_.ParameterName }}) }}
}})
$members = @($ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }}, $true) | ForEach-Object {{ $_.Member.Extent.Text }})
$redirects = @($ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst] }}, $true) | ForEach-Object {{ $_.Location.Extent.Text }})
[pscustomobject]@{{ errors = @($errors).Count; commands = $commands; members = $members; redirects = $redirects }} | ConvertTo-Json -Depth 4 -Compress
"""

_READ_VERBS = {"get", "test", "measure", "select", "sort", "where", "group", "compare", "convertto",
               "convertfrom", "find", "resolve", "join", "split"}
_READ_COMMANDS = {
    "foreach-object", "where-object", "select-object", "sort-object", "group-object", "measure-object",
    "format-table", "format-list", "format-wide", "format-custom", "out-string", "out-host", "out-null",
    "write-output", "write-host", "select-string", "new-object", "new-timespan", "start-sleep",
    "set-location", "push-location", "pop-location", "set-variable", "new-variable",
    # exécutables de consultation
    "systeminfo", "tasklist", "whoami", "hostname", "nvidia-smi", "driverquery", "ping", "ipconfig",
}
_FORBIDDEN_VERBS = {"remove", "clear", "uninstall", "install", "disable", "enable", "set", "reset",
                    "register", "unregister", "suspend", "restart", "initialize", "format", "block",
                    "revoke", "grant", "lock", "unlock", "dismount", "mount", "protect", "unprotect"}
_FORBIDDEN_COMMANDS = {
    "stop-computer", "stop-service", "invoke-expression", "add-type", "invoke-cimmethod",
    "invoke-wmimethod", "out-file", "new-localuser", "rename-computer", "checkpoint-computer",
    # exécutables : extinction, disques, registre, comptes, réglages, installations, autres interpréteurs
    "shutdown", "logoff", "format", "diskpart", "bcdedit", "reg", "net", "net1", "netsh", "sc",
    "schtasks", "takeown", "icacls", "cacls", "cipher", "del", "erase", "rd", "rmdir", "attrib",
    "cmd", "powershell", "pwsh", "wscript", "cscript", "mshta", "rundll32", "regsvr32", "msiexec",
    "winget", "choco", "scoop", "wmic", "vssadmin", "wbadmin", "manage-bde", "certutil",
    "bitsadmin", "powercfg", "fsutil", "w32tm", "reagentc", "dism", "sfc",
}
_FORBIDDEN_METHODS = {"writealltext", "writeallbytes", "writealllines", "create", "createtext",
                      "truncate", "invokemethod",
                      # objets COM qui exécutent n'importe quoi (verbe « Vider la corbeille », cmd /c del...)
                      "invokeverb", "invokeverbex", "doit", "shellexecute", "run", "exec"}
_FORBIDDEN_METHOD_STEMS = ("delete", "shutdown", "reboot", "restart", "logoff", "format", "uninstall",
                           "password")
_READ_METHODS = {"tostring", "substring", "split", "trim", "trimend", "trimstart", "replace", "contains",
                 "startswith", "endswith", "tolower", "toupper", "indexof", "join", "round", "floor",
                 "ceiling", "abs", "min", "max", "parse", "readalltext", "readalllines", "exists",
                 "adddays", "addhours", "addminutes", "opensubkey", "count", "padleft", "padright",
                 "equals"}
_INTERPRETERS = ("cmd", "powershell", "pwsh", "wscript", "cscript", "mshta", "rundll32", "regsvr32",
                 "msiexec", "reg.exe", "bash", "wsl")
_FORCE_PARAMS = {"force"}


@lru_cache(maxsize=64)
def analyze(script: str) -> tuple[Verdict, str]:
    """(verdict, raison). La raison explique un refus ou une confirmation."""
    code_file = _temp_file(".txt", script)
    try:
        out = _run(_ANALYZER.format(path=str(code_file).replace("'", "''")), timeout=20)
    finally:
        code_file.unlink(missing_ok=True)
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        return Verdict.FORBID, "analyse impossible du script"
    if info["errors"]:
        return Verdict.FORBID, "script invalide"

    verdict, reason = Verdict.ALLOW, ""

    def escalate(new: Verdict, why: str) -> None:
        nonlocal verdict, reason
        order = [Verdict.ALLOW, Verdict.CONFIRM, Verdict.FORBID]
        if order.index(new) > order.index(verdict):
            verdict, reason = new, why

    for cmd in info["commands"] or []:
        name = cmd["name"]
        if not name:
            escalate(Verdict.FORBID, "appel de commande dynamique, impossible à vérifier")
            continue
        name = name.split("\\")[-1].lower().removesuffix(".exe")
        params = {p.lower() for p in cmd["params"] or []}
        verb = name.split("-")[0] if "-" in name else ""
        if name in _FORBIDDEN_COMMANDS:
            escalate(Verdict.FORBID, "{} fait partie des interdits".format(name))
        elif name in _READ_COMMANDS or (verb in _READ_VERBS and name != "format-volume"):
            pass
        elif verb in _FORBIDDEN_VERBS:
            if name == "set-clipboard":
                escalate(Verdict.CONFIRM, "modifie le presse-papiers")
            else:
                escalate(Verdict.FORBID, "{} modifie le système ou supprime des données".format(name))
        elif name == "start-process" and "verb" in params:
            escalate(Verdict.FORBID, "élévation de droits interdite")
        elif name == "start-process" and any(i in cmd["text"].lower() for i in _INTERPRETERS):
            escalate(Verdict.FORBID, "lancer un autre interpréteur empêcherait de vérifier la commande")
        elif params & _FORCE_PARAMS:
            escalate(Verdict.FORBID, "{} -Force peut écraser des fichiers".format(name))
        else:
            escalate(Verdict.CONFIRM, "{} modifie quelque chose".format(name))

    for member in info["members"] or []:
        method = member.strip().lower()
        if not method.replace("_", "").isalnum():
            escalate(Verdict.FORBID, "appel de méthode dynamique, impossible à vérifier")
        elif method in _FORBIDDEN_METHODS or method.startswith("set") \
                or any(stem in method for stem in _FORBIDDEN_METHOD_STEMS):
            escalate(Verdict.FORBID, "la méthode {} fait partie des interdits".format(member))
        elif method not in _READ_METHODS and not method.startswith("get"):
            escalate(Verdict.CONFIRM, "la méthode {} peut modifier quelque chose".format(member))

    for location in info["redirects"] or []:
        if location.strip().lower() != "$null":
            escalate(Verdict.FORBID, "une redirection vers un fichier peut écraser son contenu")
    return verdict, reason


def _temp_file(suffix: str, content: str) -> Path:
    _TMP.mkdir(exist_ok=True)
    path = _TMP / (uuid.uuid4().hex + suffix)
    path.write_text(content, encoding="utf-8-sig")  # BOM : sinon PowerShell 5.1 lit en ANSI
    return path


def _run(code: str, timeout: float) -> str:
    """Exécute du PowerShell via un fichier .ps1 temporaire (pas de -EncodedCommand, que les
    antivirus bloquent parfois car c'est une technique courante des logiciels malveillants)."""
    script_file = _temp_file(".ps1", "$ProgressPreference = 'SilentlyContinue'\n"
                                     "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + code)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-File", str(script_file)], capture_output=True, timeout=timeout,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        script_file.unlink(missing_ok=True)
    return (out.stdout + out.stderr).decode("utf-8", errors="replace").strip()


# Sortie normale, puis un séparateur, puis le message court de chaque erreur.
_WRAPPER = """$Error.Clear()
$out = try {{ & {{
{}
}} 2>$null | Out-String -Width 160 }} catch {{ $null }}
$out
"{}"
$Error | Select-Object -First 5 | ForEach-Object {{ $_.ToString() }}
"""
_ERRORS_MARK = "@@JARVIS_ERREURS@@"


def run_powershell(script: str, goal: str) -> Result:
    verdict, reason = analyze(script)
    if verdict is Verdict.FORBID:  # double sécurité si le garde-fou a été contourné
        return Result(False, "Refusé : " + reason)
    try:
        raw = _run(_WRAPPER.format(script, _ERRORS_MARK), timeout=90)
    except subprocess.TimeoutExpired:
        return Result(False, "Le script a dépassé 90 secondes.")
    output, _, errors = raw.partition(_ERRORS_MARK)
    output, errors = output.strip(), errors.strip()
    if len(output) > 4000:
        output = output[:4000] + "\n[... sortie tronquée]"
    if not errors:
        return Result(True, output or "(aucune sortie)")
    message = "Erreurs PowerShell :\n{}\nCorrige le script et réessaie si besoin.".format(errors)
    # Des erreurs partielles (dossier inaccessible...) n'empêchent pas d'utiliser la sortie.
    return Result(bool(output), "{}\n\n{}".format(output, message) if output else message)


def guard(script: str, goal: str) -> Check:
    verdict, reason = analyze(script)
    if verdict is Verdict.FORBID:
        return forbid(reason)
    if verdict is Verdict.CONFIRM:
        return confirm("{} Je confirme ?\nScript : {}".format(goal.rstrip(".") + ".", script))
    return ALLOW


def register(box: Toolbox) -> None:
    box.add(Tool(
        "powershell",
        "Exécute un script PowerShell sur le PC, pour obtenir une information qu'aucun autre outil ne "
        "donne (fichiers et dossiers, réseau, matériel, logiciels, processus...) ou pour une action "
        "non couverte. Les scripts de lecture s'exécutent directement ; ceux qui modifient quelque "
        "chose demandent confirmation ; supprimer, éteindre, modifier les paramètres ou installer est "
        "refusé.",
        run_powershell, {
            "script": Param("script PowerShell (Windows PowerShell 5.1)"),
            "goal": Param("ce que fait le script, en une phrase en français, à la première personne "
                          "(ex. « Je liste les fichiers de ton bureau »)"),
        }, reads=True, guard=guard, notice=None))
