#!/usr/bin/env python3
"""Genera las capturas de pantalla de la interfaz para el README.

Uso:
    venv/bin/python capturar.py            # capturas con contenido de ejemplo
    venv/bin/python capturar.py --real     # abre el descargador real unos segundos
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import tkinter as tk

from interfaz import MarketplaceApp


BASE_DIR = Path(__file__).resolve().parent
SHOTS_DIR = BASE_DIR / "docs" / "capturas"
WINDOW_GEOMETRY = "1040x740+150+100"

SAMPLE_LINES = [
    "Descargador de portadas de Vehículos en Facebook Marketplace\n",
    "Salida: /Users/jayler/Desktop/TESSI-II/marketplace-downloader/marketplace_portadas\n",
    "Cargando detector visual YOLO (la primera vez descarga un modelo pequeño)...\n",
    "Cargando segundo filtro visual de clasificación...\n",
    "\nFacebook se abrió en Chromium.\n",
    "Si se solicita, inicia sesión y completa manualmente cualquier verificación.\n",
    "No cierres la ventana. El programa continuará al detectar los anuncios.\n",
    "\nLista detectada (30 publicaciones visibles inicialmente).\n",
    "Cargando todas las publicaciones mediante scroll progresivo...\n",
    "  Scroll 1: 30 candidatos; 4 descartados por texto\r",
    "  Scroll 2: 41 candidatos; 6 descartados por texto\r",
    "  Scroll 3: 52 candidatos; 9 descartados por texto\r",
    "  Scroll 4: 63 candidatos; 12 descartados por texto\r",
    "\n",
    "Se detectaron 63 publicaciones. Iniciando descargas...\n",
    "[1/63] 2020 Toyota Hilux, S/65,000, Juliaca\n",
    "    Guardada (YOLO: car 93%; clasificación: pickup 68%).\n",
    "[2/63] KIA Sportage 2019\n",
    "    Guardada (YOLO: car 88%; clasificación: minivan 54%).\n",
    "[3/63] moto usada\n",
    "    Omitida: segundo filtro: motor_scooter 41%.\n",
    "[4/63] repuesto de parachoque\n",
    "    Omitida: YOLO no detectó auto, camioneta, camión ni bus.\n",
    "[5/63] Hyundai Tucson 2021\n",
    "    Guardada (YOLO: car 91%; clasificación: minivan 61%).\n",
]


def shoot(root: tk.Tk, name: str, wait: float = 1.4) -> Path:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    root.attributes("-topmost", True)
    root.lift()
    root.update_idletasks()
    root.update()
    time.sleep(wait)
    destination = SHOTS_DIR / name
    command = [
        "screencapture",
        "-x",
        "-o",
        "-R",
        f"{root.winfo_rootx()},{root.winfo_rooty()},"
        f"{root.winfo_width()},{root.winfo_height()}",
        str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not destination.exists():
        raise RuntimeError(
            "screencapture no pudo capturar la pantalla: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    print(f"  capturada: docs/capturas/{name}")
    return destination


def main() -> int:
    real = "--real" in sys.argv
    root = tk.Tk()
    app = MarketplaceApp(root)
    root.geometry(WINDOW_GEOMETRY)

    print("Preparando la ventana...")
    deadline = time.monotonic() + 25
    while app.inicio_btn["state"] != "normal" and time.monotonic() < deadline:
        root.update()
        time.sleep(0.1)
    root.update()

    print("1/3 ventana principal")
    shoot(root, "01-ventana-principal.png")

    print("2/3 durante la descarga")
    if real:
        app.start()
        wait_until = time.monotonic() + 90
        while app.total_items < 5 and time.monotonic() < wait_until:
            root.update()
            time.sleep(0.2)
        shoot(root, "02-descargando.png")
        app.cancel()
        while app.running and time.monotonic() < wait_until + 30:
            root.update()
            time.sleep(0.2)
        root.update()
    else:
        for line in SAMPLE_LINES[:16]:
            app.handle_line(line)
        app.status_var.set("Explorando anuncios (scroll 4)...")
        app.progress.configure(maximum=63, value=4)
        shoot(root, "02-descargando.png")
        for line in SAMPLE_LINES[16:]:
            app.handle_line(line)
        app.status_var.set("Analizando y descargando 5 de 63...")
        app.progress.configure(value=5)

    print("3/3 descarga terminada")
    for line in [
        "[60/63] Chevrolet Tracker 2022\n",
        "    Guardada (YOLO: car 95%; clasificación: suv 77%).\n",
        "[61/63]Repuesto de parachoque\n",
        "    Omitida: YOLO no detectó auto, camioneta, camión ni bus.\n",
        "[62/63] Nissan Versa 2018\n",
        "    Guardada (YOLO: car 90%; clasificación: passenger_car 72%).\n",
        "[63/63] moto\n",
        "    Omitida: imagen duplicada.\n",
        "\nProceso terminado. Se descargaron 42 portadas.\n",
        "YOLO descartó 14 imágenes sin un vehículo permitido.\n",
        "Se omitieron 7 publicaciones por error o duplicado.\n",
        "CSV: /Users/jayler/Desktop/TESSI-II/marketplace-downloader/marketplace_portadas.csv\n",
    ]:
        app.handle_line(line)
    app.saved_items = 42
    app.progress.configure(value=63)
    app.status_var.set("Listo: 42 portadas descargadas.")
    shoot(root, "03-descarga-terminada.png")

    print("4/4 aviso de instalación")
    app.clear_log()
    app.apply_environment_status(
        ["Faltan paquetes de Python: ultralytics", "Falta el navegador Chromium de Playwright."]
    )
    shoot(root, "04-instalacion.png")

    root.destroy()
    print("Capturas listas en docs/capturas/")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"\nError: {exc}")
        print(
            "macOS no dejó capturar la pantalla. Ábrelo en "
            "Ajustes del Sistema > Privacidad y seguridad > Grabación de pantalla "
            "y permite el uso a la Terminal; después vuelve a intentarlo."
        )
        raise SystemExit(1)