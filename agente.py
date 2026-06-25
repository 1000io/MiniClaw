"""
MiniClaw v2 — agente local minimalista con registro de herramientas.

Añadir una herramienta nueva = decorar una función con @tool("nombre", "descripción").
"""

import json
import re
import shutil
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from functools import wraps

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =============================================================================
# CONFIGURACIÓN
# =============================================================================

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gemma4:26b"

BASE_DIR   = Path(__file__).resolve().parent
SANDBOX    = BASE_DIR / "sandbox";    SANDBOX.mkdir(exist_ok=True)
LOGS_DIR   = BASE_DIR / "logs";       LOGS_DIR.mkdir(exist_ok=True)
LOG_FILE   = LOGS_DIR / f"agente_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
PROMPT_FILE = BASE_DIR / "prompt.md"

MAX_STEPS               = 50
OLLAMA_TIMEOUT_SECONDS  = 300
COMMAND_TIMEOUT_SECONDS = 600
MAX_HISTORY_CHARS = 120_000
TRUNCATE_CHARS    = 40_000


# =============================================================================
# REGISTRO DE HERRAMIENTAS
# =============================================================================

TOOLS: dict[str, dict] = {}   # { nombre: {fn, description, params} }

def tool(name: str, description: str, params: dict | None = None):
    """
    Decorador que registra una función como herramienta disponible para el agente.

    params es un dict JSON-Schema-like:
        { "argumento": "descripción del argumento" }
    """
    def decorator(fn):
        TOOLS[name] = {
            "fn":          fn,
            "description": description,
            "params":      params or {},
        }
        @wraps(fn)
        def wrapper(*args, **kwargs):
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def tools_schema() -> str:
    """Genera la descripción de herramientas que va al system prompt."""
    lines = []
    for name, meta in TOOLS.items():
        args = ", ".join(
            f"{k}: {v}" for k, v in meta["params"].items()
        )
        lines.append(f'  "{name}"({args}) — {meta["description"]}')
    return "\n".join(lines)


def dispatch(action: str, decision: dict):
    """Ejecuta la herramienta que pide el modelo."""
    if action not in TOOLS:
        return {"error": f"Herramienta desconocida: {action!r}"}

    fn   = TOOLS[action]["fn"]
    params = {k: decision[k] for k in TOOLS[action]["params"] if k in decision}
    return fn(**params)


# =============================================================================
# HERRAMIENTAS
# =============================================================================

BLOCKED_PATTERNS = [
    r"\bRemove-Item\b", r"\brm\b", r"\bdel\b", r"\brmdir\b",
    r"\bFormat-Volume\b", r"\bdiskpart\b", r"\bbcdedit\b",
    r"\breg\s+delete\b", r"\bStop-Computer\b", r"\bRestart-Computer\b",
    r"\bshutdown\b", r"\bwinget\s+uninstall\b", r"\bRemove-Computer\b",
    r"\bClear-Disk\b", r"\bInitialize-Disk\b",
]
SUSPICIOUS_PATTERNS = [
    r"C:\\Windows\\System32", r"C:\\Windows", r"C:\\Program Files",
    r"C:\\Program Files \(x86\)", r"\$env:USERPROFILE", r"\$env:WINDIR",
]


def _check_safety(command: str) -> tuple[bool, str]:
    if not command or not command.strip():
        return False, "Comando vacío."
    for p in BLOCKED_PATTERNS:
        if re.search(p, command, re.IGNORECASE):
            return False, f"Bloqueado: {p}"
    for p in SUSPICIOUS_PATTERNS:
        if re.search(p, command, re.IGNORECASE):
            return False, f"Ruta sensible bloqueada: {p}"
    return True, ""


def _truncate(text: str | None, max_chars: int = TRUNCATE_CHARS) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[TRUNCADO]...\n" + text[-2000:]


@tool(
    "run_command",
    "Ejecuta un comando PowerShell en el sandbox.",
    {"command": "código PowerShell a ejecutar"},
)
def run_command(command: str) -> dict:
    ok, reason = _check_safety(command)
    if not ok:
        return {"exit_code": 999, "stdout": "", "stderr": reason}

    shell = next((c for c in ("pwsh", "powershell") if shutil.which(c)), "powershell")
    log_event("CMD", command)

    try:
        r = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=str(SANDBOX), text=True, capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        result = {
            "exit_code": r.returncode,
            "stdout":    _truncate(r.stdout),
            "stderr":    _truncate(r.stderr),
        }
    except subprocess.TimeoutExpired:
        result = {"exit_code": 998, "stdout": "", "stderr": "Timeout."}
    except Exception as exc:
        result = {"exit_code": 996, "stdout": "", "stderr": str(exc)}

    log_event("CMD_RESULT", result)
    return result


@tool(
    "read_file",
    "Lee un archivo de texto del sandbox y devuelve su contenido.",
    {"path": "ruta relativa al sandbox"},
)
def read_file(path: str) -> dict:
    try:
        target = (SANDBOX / path).resolve()
        if not str(target).startswith(str(SANDBOX)):
            return {"error": "Ruta fuera del sandbox."}
        content = target.read_text(encoding="utf-8", errors="replace")
        return {"content": _truncate(content), "chars": len(content)}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    "write_file",
    "Crea o sobreescribe un archivo de texto en el sandbox.",
    {"path": "ruta relativa", "content": "contenido a escribir"},
)
def write_file(path: str, content: str) -> dict:
    try:
        target = (SANDBOX / path).resolve()
        if not str(target).startswith(str(SANDBOX)):
            return {"error": "Ruta fuera del sandbox."}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"ok": True, "bytes": len(content.encode())}
    except Exception as exc:
        return {"error": str(exc)}


@tool(
    "fetch_url",
    "Descarga el contenido de una URL (texto/HTML). Útil para leer docs o APIs.",
    {"url": "URL a descargar", "save_as": "(opcional) nombre de archivo en sandbox"},
)
def fetch_url(url: str, save_as: str = "") -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MiniClaw/2.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            ct  = resp.headers.get_content_charset("utf-8")
            try:
                text = raw.decode(ct, errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")

        result: dict = {"url": url, "length": len(text), "content": _truncate(text, 10_000)}

        if save_as:
            target = (SANDBOX / save_as).resolve()
            if str(target).startswith(str(SANDBOX)):
                target.write_text(text, encoding="utf-8")
                result["saved_as"] = save_as

        return result
    except Exception as exc:
        return {"error": str(exc)}


# =============================================================================
# PROMPT DINÁMICO
# =============================================================================

def build_system_prompt() -> str:
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"No se encuentra: {PROMPT_FILE}")

    base = PROMPT_FILE.read_text(encoding="utf-8")
    base = base.replace("{SANDBOX}", str(SANDBOX))

    # Reemplaza el bloque de herramientas del prompt con el registro real.
    tool_block = "Herramientas disponibles:\n" + tools_schema()
    if "{TOOLS}" in base:
        return base.replace("{TOOLS}", tool_block)

    # Si el prompt no tiene el marcador, añadelo al inicio.
    return tool_block + "\n\n" + base


# =============================================================================
# LOGGING
# =============================================================================

def log_event(title: str, data=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n[{ts}] {title}\n{'='*60}\n")
        if data is not None:
            f.write(json.dumps(data, indent=2, ensure_ascii=False)
                    if isinstance(data, (dict, list)) else str(data))
            f.write("\n")


# =============================================================================
# OLLAMA
# =============================================================================

def ask_ollama(prompt: str) -> str:
    payload = {"model": MODEL, "prompt": prompt, "stream": False,
               "options": {
                    "temperature": 0.1,
                    "num_ctx": 8192,        # <-- añade esto
                    "num_predict": 4096,    # <-- y esto
                }
                }
    log_event("LLM_REQUEST", payload)

    try:
        r = requests.post(OLLAMA_URL, json=payload, verify=False,
                          timeout=OLLAMA_TIMEOUT_SECONDS)
        r.raise_for_status()
        data = r.json()
        log_event("LLM_RESPONSE", data)
        return data.get("response", "")
    except requests.exceptions.Timeout:
        return _make_final(f"Timeout tras {OLLAMA_TIMEOUT_SECONDS}s.")
    except Exception as exc:
        return _make_final(f"Error llamando al modelo: {exc}")


def _make_final(msg: str) -> str:
    return json.dumps({"action": "final", "message": msg}, ensure_ascii=False)


# =============================================================================
# JSON EXTRACTOR (robusto)
# =============================================================================

def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()

    # Intento 1: JSON directo.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Intento 2: primer bloque { … }.
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Intento 3: el modelo a veces olvida cerrar llaves.
    candidate = text[start:] if start >= 0 else text
    try:
        return json.loads(candidate + "}")
    except Exception:
        pass

    raise ValueError(f"No se pudo extraer JSON de:\n{text[:300]}")


# =============================================================================
# HISTORIAL
# =============================================================================

def compact_history(history: list[str]) -> list[str]:
    total = sum(len(h) for h in history)
    if total <= MAX_HISTORY_CHARS:
        return history

    fixed, tail = history[:2], history[2:]
    note = "SYSTEM NOTE: historial recortado para no superar el límite de contexto."

    while tail and sum(len(h) for h in fixed + [note] + tail) > MAX_HISTORY_CHARS:
        tail.pop(0)

    return fixed + [note] + tail


# =============================================================================
# BUCLE PRINCIPAL
# =============================================================================

def main():
    system_prompt = build_system_prompt()

    log_event("INICIO", {"model": MODEL, "sandbox": str(SANDBOX),
                         "tools": list(TOOLS.keys())})

    user_task = input("Tarea para el agente: ").strip()
    if not user_task:
        print("Sin tarea.")
        return

    log_event("TAREA", user_task)

    history = [
        "SYSTEM:\n" + system_prompt,
        "USER:\n" + user_task,
    ]

    for step in range(1, MAX_STEPS + 1):
        history    = compact_history(history)
        full_prompt = "\n\n".join(history) + "\n\nASSISTANT JSON:"

        log_event(f"PASO_{step}_PROMPT", full_prompt)
        print(f"\n--- Paso {step} ---")

        raw = ask_ollama(full_prompt)
        log_event(f"PASO_{step}_RAW", raw)
        print(raw)

        # --- Parsear JSON ---
        try:
            decision = extract_json(raw)
        except Exception as exc:
            print(f"❌ JSON inválido: {exc}")
            # Pedimos al modelo que lo corrija una vez.
            history.append(
                "SYSTEM NOTE: tu respuesta no era JSON válido. "
                "Responde SOLO con JSON, sin markdown ni explicaciones."
            )
            continue

        action = decision.get("action", "")
        log_event(f"PASO_{step}_ACTION", action)

        if action == "final":
            print("\n✅ FINAL:")
            print(decision.get("message", "Completado."))
            break

        if action not in TOOLS:
            print(f"❌ Acción desconocida: {action!r}")
            history.append(
                f"SYSTEM NOTE: acción '{action}' no existe. "
                f"Usa solo: {list(TOOLS.keys())} o 'final'."
            )
            continue

        # --- Ejecutar herramienta ---
        result = dispatch(action, decision)

        print(f"\n🔧 {action}:")
        print(json.dumps(result, indent=2, ensure_ascii=False))

        history.append("ASSISTANT:\n" + json.dumps(decision, ensure_ascii=False))
        history.append("TOOL_RESULT:\n"  + json.dumps(result,   ensure_ascii=False))

    else:
        print(f"\n⚠️ Límite de {MAX_STEPS} pasos alcanzado.")


if __name__ == "__main__":
    main()
