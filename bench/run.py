"""Benchmark de l'agent : le LLM choisit-il le bon premier outil, avec les bons arguments ?

Rien n'est exécuté : seul le premier message du modèle est examiné (Agent.first_step).
Mesure aussi la part d'appels « natifs » (format d'outil d'Ollama) face aux appels écrits
en texte que Jarvis doit récupérer.

Usage :
    python -m bench.run                        # modèle de config.yaml
    python -m bench.run --model qwen3:14b      # autre modèle Ollama
    python -m bench.run --only steam           # catégories dont le nom contient « steam »
    python -m bench.run --save                 # enregistre bench/results/agent/<modèle>.json
    python -m bench.run --model-only           # sans les commandes directes : mesure le modèle seul

Les commandes fréquentes (« pause », « monte le son »...) sont reconnues sans le modèle
(jarvis/shortcuts.py) : elles comptent dans le score, comme dans Jarvis.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
from pathlib import Path
from unittest import mock

import yaml

from jarvis.agent import _parse_args
from jarvis.engine import Engine
from jarvis.tools import norm
from jarvis.tools import windows as win
from jarvis.tools.apps import AppCatalog

BENCH_DIR = Path(__file__).resolve().parent
BENCH_FACTS = [("prénom", "Dan"), ("anniversaire de maman", "12 mars")]


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _window(spec: str | None) -> win.Window | None:
    if not spec:
        return None
    process, _, title = spec.partition("|")
    return win.Window(0, title, 0, process)


def check(case: dict, message: dict, catalog: AppCatalog) -> str | None:
    """Raison de l'échec, ou None si le premier appel est correct."""
    expected = _as_list(case["tool"])
    calls = message["tool_calls"]
    if not calls:
        if "none" not in expected:
            return "aucun outil"
        answer = norm(message.get("content") or "")
        for requirement in case.get("answer", []):
            if not any(norm(word) in answer for word in _as_list(requirement)):
                return "réponse sans « {} »".format(requirement)
        return None
    call = calls[0]["function"]
    if call["name"] not in expected:
        return "outil {}".format(call["name"])
    try:
        args = _parse_args(call.get("arguments"))
    except ValueError:
        return "arguments illisibles"
    if call["name"] == "open_app" and "steam_launch" in expected:  # open_app lance les jeux Steam par Steam
        args = {"game": args.get("name", ""), **args}
    if "app" in case and call["name"] == "open_app":  # un autre outil accepté n'a pas de nom d'appli
        found = catalog.find(str(args.get("name", "")))
        label = (found[1].get("name") or found[1].get("Name")) if found else ""
        if norm(case["app"]) not in norm(label):
            return "app {!r}".format(args.get("name"))
    for key, requirements in (case.get("args") or {}).items():
        value = norm(args.get(key, ""))
        for requirement in requirements:
            if not any(norm(word) in value for word in _as_list(requirement)):
                return "{} {!r}".format(key, args.get(key))
    return None


def _describe(message: dict) -> str:
    calls = message["tool_calls"]
    if not calls:
        return "texte : " + (message.get("content") or "").replace("\n", " ")[:100]
    return " + ".join("{}({})".format(c["function"]["name"], json.dumps(c["function"].get("arguments"),
                                                                       ensure_ascii=False)) for c in calls)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--model", help="modèle Ollama à tester (défaut : celui de config.yaml)")
    parser.add_argument("--only", help="ne garde que les catégories dont le nom contient ce texte")
    parser.add_argument("--save", action="store_true", help="enregistre le résultat dans bench/results/agent/")
    parser.add_argument("--model-only", action="store_true", help="sans les commandes directes (modèle seul)")
    opts = parser.parse_args()

    engine = Engine()
    logging.getLogger("jarvis").setLevel(logging.ERROR)
    if opts.model:
        engine.llm.model = opts.model
    error = engine.llm.check()
    if error:
        raise SystemExit(error)
    engine.llm.warm_up()
    engine.catalog.load_start_menu()
    engine.agent.facts = lambda: list(BENCH_FACTS)  # mémoire fixe : le score ne dépend pas de la vraie base
    if opts.model_only:
        engine.agent.shortcut = lambda text: None

    categories = yaml.safe_load((BENCH_DIR / "cases.yaml").read_text(encoding="utf-8"))
    cases = [(cat, case) for cat, items in categories.items() if not opts.only or opts.only in cat
             for case in items]
    results: dict[str, list[bool]] = {}
    failures, latencies, native, direct = [], [], [], 0

    for i, (category, case) in enumerate(cases, 1):
        history = case.get("history", [])
        with mock.patch.object(win, "current_window", return_value=_window(case.get("window"))):
            start = time.perf_counter()
            message = engine.agent.first_step(case["say"], history)
            latencies.append(time.perf_counter() - start)
        direct += bool(message.get("shortcut"))
        if message["tool_calls"] and not message.get("shortcut"):
            native.append(message["native"])
        reason = check(case, message, engine.catalog)
        results.setdefault(category, []).append(reason is None)
        if reason:
            failures.append({"category": category, "say": case["say"], "expected": case["tool"],
                             "reason": reason, "got": _describe(message), "native": message["native"]})
        print("\r{}/{}".format(i, len(cases)), end="", flush=True)

    passed = len(cases) - len(failures)
    latencies_ms = sorted(round(l * 1000) for l in latencies)
    summary = {
        "model": engine.llm.model,
        "accuracy": round(100 * passed / len(cases), 1),
        "passed": passed,
        "total": len(cases),
        "native_calls": round(100 * sum(native) / len(native), 1) if native else None,
        "shortcuts": direct,
        "latency_ms_mean": round(statistics.mean(latencies_ms)),
        "latency_ms_p95": latencies_ms[int(0.95 * (len(latencies_ms) - 1))],
        "by_category": {cat: "{}/{}".format(sum(r), len(r)) for cat, r in results.items()},
    }

    print("\n\nModèle {model} : {accuracy} % ({passed}/{total})  |  commandes directes {shortcuts}  |  appels natifs "
          "{native_calls} %  |  latence moyenne {latency_ms_mean} ms, p95 {latency_ms_p95} ms\n".format(**summary))
    for category, score in summary["by_category"].items():
        print("  {:<16} {}".format(category, score))
    if failures:
        print("\nÉchecs :")
        for f in failures:
            print("  [{}] « {} » attendu {} -> {}{}  [{}]".format(
                f["category"], f["say"], f["expected"], f["got"][:140],
                "" if f["native"] or f["got"].startswith("texte") else " (écrit en texte)", f["reason"]))

    if opts.save:
        name = engine.llm.model.replace(":", "_") + ("_modele_seul" if opts.model_only else "")
        out = BENCH_DIR / "results" / "agent" / (name + ".json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"summary": summary, "failures": failures}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print("\nRésultat enregistré dans", out)


if __name__ == "__main__":
    main()
