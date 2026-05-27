import json
import re
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =============================================================================
# CONFIGURACIÓN
# =============================================================================

OLLAMA_URL = "https://servidor.ollama/api/generate"

# Puedes cambiarlo aquí o usar otra línea comentada.
MODEL = "gemma4:26b"

BASE_DIR = Path(__file__).resolve().parent
SANDBOX = BASE_DIR / "sandbox"
SANDBOX.mkdir(exist_ok=True)

LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

LOG_FILE = LOGS_DIR / f"agente_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

PROMPT_FILE = BASE_DIR / "prompt.md"

MAX_STEPS = 50
OLLAMA_TIMEOUT_SECONDS = 300
COMMAND_TIMEOUT_SECONDS = 600

# Limita cuánto historial se reenvía al modelo. Evita prompts enormes.
MAX_HISTORY_CHARS = 60_000


# =============================================================================
# PROMPT
# =============================================================================

def load_system_prompt() -> str:
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"No existe el archivo de prompt: {PROMPT_FILE}")

    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    return prompt.replace("{SANDBOX}", str(SANDBOX))


SYSTEM_PROMPT = load_system_prompt()


# =============================================================================
# LOGGING
# =============================================================================

def log_event(title: str, data=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write("\n" + "=" * 80 + "\n")
        f.write(f"[{timestamp}] {title}\n")
        f.write("=" * 80 + "\n")

        if data is not None:
            if isinstance(data, (dict, list)):
                f.write(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                f.write(str(data))

            f.write("\n")


# =============================================================================
# UTILIDADES
# =============================================================================

def make_final(message: str) -> str:
    return json.dumps(
        {
            "action": "final",
            "message": message
        },
        ensure_ascii=False
    )


def find_powershell_executable() -> str:
    """
    Intenta usar PowerShell moderno si existe; si no, usa powershell clásico.
    """
    for candidate in ("pwsh", "powershell"):
        path = shutil.which(candidate)
        if path:
            return candidate

    # En Windows normalmente existe aunque shutil.which no lo encuentre.
    return "powershell"


def truncate_text(text: str, max_chars: int = 20_000) -> str:
    if text is None:
        return ""

    text = str(text)

    if len(text) <= max_chars:
        return text

    return (
        text[:max_chars]
        + "\n\n...[SALIDA TRUNCADA POR EL AGENTE]...\n\n"
        + text[-2000:]
    )


def compact_history(history: list[str]) -> list[str]:
    """
    Mantiene el prompt dentro de un tamaño razonable.
    Conserva siempre el SYSTEM y la tarea inicial, y recorta por la izquierda
    las entradas intermedias si el historial crece demasiado.
    """
    if not history:
        return history

    total = sum(len(item) for item in history)
    if total <= MAX_HISTORY_CHARS:
        return history

    fixed = history[:2]
    tail = history[2:]

    while tail and sum(len(item) for item in fixed + tail) > MAX_HISTORY_CHARS:
        tail.pop(0)

    return fixed + ["SYSTEM NOTE:\nSe ha recortado historial antiguo para no superar el límite de contexto."] + tail


# =============================================================================
# OLLAMA
# =============================================================================

def ask_ollama(prompt: str) -> str:
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1
        }
    }

    log_event("LLAMADA AL LLM - PAYLOAD", payload)

    try:
        response = requests.post(
            OLLAMA_URL,
            json=payload,
            verify=False,
            timeout=OLLAMA_TIMEOUT_SECONDS
        )
        response.raise_for_status()

        response_json = response.json()
        log_event("RESPUESTA RAW DEL LLM", response_json)

        if "response" not in response_json:
            return make_final(
                "La respuesta de Ollama no contiene el campo esperado 'response'. "
                "Revisa el log para ver la respuesta completa."
            )

        return response_json["response"]

    except requests.exceptions.Timeout:
        log_event("ERROR TIMEOUT OLLAMA", {"timeout": OLLAMA_TIMEOUT_SECONDS})
        return make_final(
            f"La llamada al modelo ha excedido {OLLAMA_TIMEOUT_SECONDS} segundos. "
            "Prueba con un modelo más rápido, reduce el historial o reintenta la tarea."
        )

    except requests.exceptions.RequestException as exc:
        log_event("ERROR LLAMANDO A OLLAMA", str(exc))
        return make_final(f"Error llamando a Ollama: {exc}")

    except Exception as exc:
        log_event("ERROR INESPERADO EN ask_ollama", str(exc))
        return make_final(f"Error inesperado llamando al modelo: {exc}")


# =============================================================================
# JSON
# =============================================================================

def extract_json(text: str) -> dict:
    """
    Extrae JSON incluso si el modelo lo devuelve dentro de ```json ... ```.
    """
    text = text.strip()

    # Quitar fences markdown si aparecen.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")

        if start >= 0 and end > start:
            candidate = text[start:end + 1]
            return json.loads(candidate)

        raise


# =============================================================================
# SEGURIDAD DE COMANDOS
# =============================================================================

BLOCKED_PATTERNS = [
    r"\bRemove-Item\b",
    r"\brm\b",
    r"\bdel\b",
    r"\brmdir\b",
    r"\bFormat-Volume\b",
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\breg\s+delete\b",
    r"\bStop-Computer\b",
    r"\bRestart-Computer\b",
    r"\bshutdown\b",
    r"\bwinget\s+uninstall\b",
    r"\bRemove-Computer\b",
    r"\bClear-Disk\b",
    r"\bInitialize-Disk\b",
]

SUSPICIOUS_PATTERNS = [
    r"C:\\Windows\\System32",
    r"C:\\Windows",
    r"C:\\Program Files",
    r"C:\\Program Files \(x86\)",
    r"\$env:USERPROFILE",
    r"\$env:WINDIR",
]


def check_command_safety(command: str) -> tuple[bool, str]:
    """
    Devuelve:
      (True, "") si el comando parece aceptable.
      (False, motivo) si debe bloquearse.

    Este filtro evita falsos positivos como 'model = ...', porque usa límites
    de palabra en vez de buscar substrings como 'del '.
    """
    if not command or not command.strip():
        return False, "Comando vacío."

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return False, f"Comando bloqueado por seguridad: {pattern}"

    # No bloqueamos siempre las rutas absolutas, porque a veces son necesarias,
    # pero sí impedimos rutas claramente peligrosas.
    for pattern in SUSPICIOUS_PATTERNS:
        if re.search(pattern, command, flags=re.IGNORECASE):
            return False, f"Comando bloqueado por usar ruta sensible: {pattern}"

    return True, ""


# =============================================================================
# HERRAMIENTA: run_command
# =============================================================================

def run_command(command: str) -> dict:
    is_safe, reason = check_command_safety(command)

    if not is_safe:
        result = {
            "command": command,
            "exit_code": 999,
            "stdout": "",
            "stderr": reason
        }

        log_event("COMANDO BLOQUEADO", result)
        return result

    log_event("EJECUTANDO COMANDO POWERSHELL", command)

    shell = find_powershell_executable()

    try:
        completed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command
            ],
            cwd=str(SANDBOX),
            text=True,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS
        )

        result = {
            "command": command,
            "exit_code": completed.returncode,
            "stdout": truncate_text(completed.stdout),
            "stderr": truncate_text(completed.stderr)
        }

        log_event("RESPUESTA DEL SHELL", result)
        return result

    except subprocess.TimeoutExpired as exc:
        result = {
            "command": command,
            "exit_code": 998,
            "stdout": truncate_text(exc.stdout),
            "stderr": (
                f"Timeout ejecutando comando tras {COMMAND_TIMEOUT_SECONDS} segundos. "
                "El proceso fue detenido."
            )
        }

        log_event("TIMEOUT DEL SHELL", result)
        return result

    except FileNotFoundError as exc:
        result = {
            "command": command,
            "exit_code": 997,
            "stdout": "",
            "stderr": f"No se encontró PowerShell/pwsh: {exc}"
        }

        log_event("ERROR SHELL NO ENCONTRADO", result)
        return result

    except Exception as exc:
        result = {
            "command": command,
            "exit_code": 996,
            "stdout": "",
            "stderr": f"Error inesperado ejecutando comando: {exc}"
        }

        log_event("ERROR INESPERADO DEL SHELL", result)
        return result


# =============================================================================
# BUCLE PRINCIPAL
# =============================================================================

def main():
    log_event("INICIO DEL AGENTE", {
        "model": MODEL,
        "ollama_url": OLLAMA_URL,
        "sandbox": str(SANDBOX),
        "prompt_file": str(PROMPT_FILE),
        "log_file": str(LOG_FILE),
        "max_steps": MAX_STEPS,
        "ollama_timeout_seconds": OLLAMA_TIMEOUT_SECONDS,
        "command_timeout_seconds": COMMAND_TIMEOUT_SECONDS
    })

    user_task = input("Tarea para el agente: ").strip()

    if not user_task:
        print("No se ha introducido ninguna tarea.")
        return

    log_event("TAREA DEL USUARIO", user_task)

    history = [
        "SYSTEM:\n" + SYSTEM_PROMPT,
        "USER:\n" + user_task
    ]

    log_event("HISTORIAL INICIAL", history)

    for step in range(1, MAX_STEPS + 1):
        history = compact_history(history)

        full_prompt = "\n\n".join(history) + "\n\nASSISTANT JSON:"

        log_event(f"PASO {step} - PROMPT COMPLETO ENVIADO AL LLM", full_prompt)

        print(f"\n--- Paso {step} ---")

        raw = ask_ollama(full_prompt)

        log_event(f"PASO {step} - TEXTO DEVUELTO POR EL MODELO", raw)

        print("Modelo:")
        print(raw)

        try:
            decision = extract_json(raw)
            log_event(f"PASO {step} - JSON INTERPRETADO", decision)
        except Exception as exc:
            log_event(f"PASO {step} - ERROR INTERPRETANDO JSON", {
                "raw": raw,
                "error": str(exc)
            })

            print("No pude interpretar JSON del modelo.")
            print(exc)
            break

        action = decision.get("action")

        log_event(f"PASO {step} - ACCIÓN DECIDIDA", action)

        if action == "run_command":
            command = decision.get("command", "")

            if not command:
                log_event(f"PASO {step} - ERROR", "El modelo pidió run_command sin command.")
                print("El modelo pidió run_command sin command.")
                break

            print("\nEjecutando PowerShell:")
            print(command)

            result = run_command(command)

            print("\nResultado:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

            history.append("ASSISTANT:\n" + json.dumps(decision, ensure_ascii=False))
            history.append("TOOL_RESULT:\n" + json.dumps(result, ensure_ascii=False))

            log_event(f"PASO {step} - HISTORIAL ACTUALIZADO", history)

        elif action == "final":
            message = decision.get("message", "Terminado.")

            log_event("FINAL DEL AGENTE", message)

            print("\nFINAL:")
            print(message)
            break

        else:
            log_event(f"PASO {step} - ACCIÓN DESCONOCIDA", decision)

            print("Acción desconocida:")
            print(decision)
            break

    else:
        log_event("LÍMITE DE PASOS ALCANZADO", {
            "max_steps": MAX_STEPS
        })

        print("Límite de pasos alcanzado.")


if __name__ == "__main__":
    main()
