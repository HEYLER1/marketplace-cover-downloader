#!/usr/bin/env python3
"""Interfaz gráfica del descargador de portadas de Facebook Marketplace."""

from __future__ import annotations

import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


BASE_DIR = Path(__file__).resolve().parent
MAIN_PY = BASE_DIR / "main.py"
VENV_DIR = BASE_DIR / "venv"
VENV_PY = VENV_DIR / "bin" / "python"
INSTALL_SCRIPT = BASE_DIR / "instalar.sh"
OUTPUT_DIR = BASE_DIR / "marketplace_portadas"
CSV_PATH = BASE_DIR / "marketplace_portadas.csv"
SESSION_DIR = BASE_DIR / "facebook_session"

REQUIRED_PACKAGES = ("playwright", "ultralytics", "PIL")
LOG_LIMIT = 8000
GRACE_SECONDS = 6.0

RE_SCROLL = re.compile(r"^Scroll (\d+):")
RE_DETECTED = re.compile(r"Se detectaron (\d+) publicaciones")
RE_PROGRESS = re.compile(r"^\[(\d+)/(\d+)\]")
RE_DONE = re.compile(r"Se descargaron (\d+) portadas")
RE_VISUAL = re.compile(r"YOLO descartó (\d+) imágenes")


def venv_python_ok() -> bool:
    return VENV_PY.exists()


def missing_requirements() -> list[str]:
    problems: list[str] = []
    if not venv_python_ok():
        return ["No existe el entorno virtual (carpeta venv)."]
    probe = (
        "import importlib.util, sys\n"
        "missing = [name for name in {names!r} if importlib.util.find_spec(name) is None]\n"
        "print(','.join(missing))\n"
        "sys.exit(1 if missing else 0)\n"
    ).format(names=list(REQUIRED_PACKAGES))
    try:
        result = subprocess.run(
            [str(VENV_PY), "-c", probe],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ["No se puede ejecutar Python del entorno virtual."]
    if result.returncode != 0:
        names = (result.stdout or "").strip().splitlines()
        problems.append(f"Faltan paquetes de Python: {names[-1] if names else 'desconocidos'}")
    if not has_chromium():
        problems.append("Falta el navegador Chromium de Playwright.")
    return problems


def has_chromium() -> bool:
    cache = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        or Path.home() / "Library" / "Caches" / "ms-playwright"
    )
    if not cache.is_dir():
        return False
    return any(entry.name.startswith("chromium-") for entry in cache.iterdir())


class MarketplaceApp:
    """Ventana principal: ejecuta main.py y muestra su salida en vivo."""

    POLL_MS = 120

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Descargador de portadas - Facebook Marketplace")
        self.root.geometry("960x700")
        self.root.minsize(780, 540)

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.process: subprocess.Popen | None = None
        self.cancelled = False
        self.mode = ""
        self.partial_at: str | None = None
        self.total_items = 0
        self.saved_items = 0

        self._configure_style()
        self._build_widgets()
        self.root.after(self.POLL_MS, self._poll_events)
        self.root.after(300, self.check_environment)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_buttons()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("aqua", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Titulo.TLabel", font=("Helvetica Neue", 17, "bold"))
        style.configure("Sub.TLabel", font=("Helvetica Neue", 11), foreground="#555555")
        style.configure("Estado.TLabel", font=("Helvetica Neue", 12, "bold"))
        style.configure("Campo.TButton", font=("Helvetica Neue", 13, "bold"), padding=(18, 11))
        style.configure("Sec.TButton", font=("Helvetica Neue", 11), padding=(10, 6))
        style.configure("Aviso.TLabelframe.Label", font=("Helvetica Neue", 11, "bold"))

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Descargador de portadas de Vehículos", style="Titulo.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            outer,
            text=(
                "Guarda en marketplace_portadas/ una portada por anuncio y genera "
                "marketplace_portadas.csv. Siempre se aplican los filtros de texto y de IA (YOLO)."
            ),
            style="Sub.TLabel",
            wraplength=880,
            justify="left",
        ).pack(anchor="w", pady=(2, 14))

        acciones = ttk.Frame(outer)
        acciones.pack(fill="x")
        self.inicio_btn = ttk.Button(
            acciones, text="▶  Iniciar descarga", style="Campo.TButton", command=self.start
        )
        self.inicio_btn.pack(side="left")
        self.cancelar_btn = ttk.Button(
            acciones, text="■  Cancelar", style="Campo.TButton", command=self.cancel
        )
        self.cancelar_btn.pack(side="left", padx=10)
        self.progress = ttk.Progressbar(acciones, mode="determinate", maximum=100, length=180)
        self.progress.pack(side="left", padx=(18, 0))

        self.status_var = tk.StringVar(value="Listo para comenzar.")
        ttk.Label(
            outer, textvariable=self.status_var, style="Estado.TLabel", wraplength=860,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

        self.aviso = ttk.Labelframe(outer, text="Preparación necesaria", style="Aviso.TLabelframe")
        self.aviso_text = tk.StringVar(value="")
        ttk.Label(self.aviso, textvariable=self.aviso_text, wraplength=640, justify="left").pack(
            side="left", padx=10, pady=8
        )
        self.instalar_btn = ttk.Button(
            self.aviso, text="Instalar ahora", style="Sec.TButton", command=self.install
        )
        self.instalar_btn.pack(side="right", padx=10)

        self.reiniciar_sesion = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            outer,
            text="Cerrar antes la sesión de Facebook guardada, si siempre te pide iniciar sesión",
            variable=self.reiniciar_sesion,
        ).pack(anchor="w", pady=(10, 0))

        self.log_frame = ttk.LabelFrame(outer, text="Registro de actividad", padding=8)
        self.log_frame.pack(fill="both", expand=True, pady=12)
        self.log_frame.rowconfigure(0, weight=1)
        self.log_frame.columnconfigure(0, weight=1)

        self.log = tk.Text(
            self.log_frame,
            wrap="word",
            font=("Menlo", 11),
            background="#11151c",
            foreground="#dfe6ef",
            insertbackground="#dfe6ef",
            relief="flat",
            padx=10,
            pady=8,
        )
        scroll = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.tag_configure("ok", foreground="#63d68a")
        self.log.tag_configure("warn", foreground="#f0b350")
        self.log.tag_configure("err", foreground="#ff7b72")
        self.log.tag_configure("titulo", foreground="#79b8ff", font=("Menlo", 11, "bold"))

        extras = ttk.Frame(outer)
        extras.pack(fill="x")
        ttk.Button(extras, text="Limpiar registro", style="Sec.TButton", command=self.clear_log).pack(
            side="left"
        )
        ttk.Button(
            extras, text="Abrir imágenes", style="Sec.TButton", command=lambda: self.reveal(OUTPUT_DIR)
        ).pack(side="left", padx=8)
        ttk.Button(
            extras, text="Abrir CSV", style="Sec.TButton", command=lambda: self.reveal(CSV_PATH)
        ).pack(side="left")

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def refresh_buttons(self) -> None:
        self.inicio_btn.configure(state="disabled" if self.running else "normal")
        self.cancelar_btn.configure(state="normal" if self.running else "disabled")
        self.instalar_btn.configure(
            state="disabled" if self.running or self.mode == "instalar" else "normal"
        )

    def emit(self, line: str, tag: str | None = None) -> None:
        self.events.put(("line", (line, tag)))

    def check_environment(self) -> None:
        def worker() -> None:
            self.events.put(("deps", missing_requirements()))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "line":
                    line, tag = payload  # type: ignore[misc]
                    self.handle_line(str(line), tag)  # type: ignore[arg-type]
                elif kind == "exit":
                    self.handle_exit(int(payload))  # type: ignore[arg-type]
                elif kind == "deps":
                    self.apply_environment_status(list(payload))  # type: ignore[arg-type]
        except queue.Empty:
            pass
        self.root.after(self.POLL_MS, self._poll_events)

    def apply_environment_status(self, problems: list[str]) -> None:
        if self.running or self.mode == "instalar":
            self.root.after(700, lambda: self.apply_environment_status(problems))
            return
        if problems:
            detail = " ".join(f"• {problem}" for problem in problems)
            self.aviso_text.set(f"{detail}\nPulsa «Instalar ahora» y espera a que termine.")
            self.aviso.pack(fill="x", pady=(12, 0), before=self.log_frame)
            self.status_var.set("Falta preparar el entorno.")
            self.inicio_btn.configure(state="disabled")
            self.emit("Entorno incompleto: " + "; ".join(problems), "err")
        else:
            self.aviso.pack_forget()
            self.inicio_btn.configure(state="normal")
            if self.status_var.get() == "Falta preparar el entorno.":
                self.status_var.set("Listo para comenzar.")

    def _enable_log(self) -> None:
        self.log.configure(state="normal")
        if len(self.log.get("1.0", "end-1c")) > LOG_LIMIT:
            self.log.delete("1.0", "80.0")
            self.partial_at = None

    def _disable_log(self) -> None:
        self.log.configure(state="disabled")

    def _show_partial(self, text: str) -> None:
        """Muestra un segmento que el siguiente carriage return va a reemplazar."""
        self.end_partial()
        self._enable_log()
        self.partial_at = self.log.index("end-1c")
        self.log.insert("end-1c", text)
        self.log.see("end")
        self._disable_log()

    def _commit_partial(self, tag: str | None = None) -> None:
        """Cierra la línea actual conservando lo que ya se ve en pantalla."""
        self._enable_log()
        if self.partial_at is not None:
            if tag:
                self.log.tag_add(tag, self.partial_at, "end-1c")
            self.partial_at = None
        self.log.insert("end-1c", "\n")
        self.log.see("end")
        self._disable_log()

    def end_partial(self) -> None:
        if self.partial_at is None:
            return
        self._enable_log()
        self.log.delete(self.partial_at, "end-1c")
        self.partial_at = None
        self._disable_log()

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.partial_at = None

    def handle_line(self, line: str, tag: str | None = None) -> None:
        if not line:
            return
        complete = line.endswith("\n")
        body = line[:-1] if complete else line
        segments = [segment for segment in body.split("\r") if segment]
        for segment in segments:
            self._show_partial(segment)
        if not segments:
            if complete:
                self._commit_partial()
            return
        self.line_tag = self.classify(segments[-1], tag)
        if complete:
            self._commit_partial(self.line_tag)

    def classify(self, line: str, forced_tag: str | None = None) -> str | None:
        stripped = line.strip()
        if not stripped:
            return None
        detected = RE_DETECTED.search(stripped)
        if detected:
            self.total_items = int(detected.group(1))
            self.progress.configure(maximum=max(self.total_items, 1))
            return "titulo"
        scroll = RE_SCROLL.match(stripped)
        if scroll:
            self.status_var.set(f"Explorando anuncios (scroll {scroll.group(1)})...")
            return None
        progress = RE_PROGRESS.match(stripped)
        if progress:
            position, total = int(progress.group(1)), int(progress.group(2))
            self.saved_items = position
            self.total_items = total
            self.progress.configure(maximum=max(total, 1), value=position)
            self.status_var.set(f"Analizando y descargando {position} de {total}...")
            return None
        done = RE_DONE.search(stripped)
        if done:
            self.saved_items = int(done.group(1))
            self.progress.configure(value=self.progress["maximum"])
            return "titulo"
        if RE_VISUAL.search(stripped):
            return "warn"
        lowered = stripped.lower()
        if lowered.startswith(("error", "aviso")):
            return "err"
        if stripped.startswith(("Omitida", "Proceso cancelado")):
            return "warn"
        if stripped.startswith(("Guardada", "Proceso terminado")):
            return "ok"
        if stripped.startswith(("Facebook se", "Cargando", "Descargador", "Iniciando", "Instal")):
            return "titulo"
        return forced_tag

    def install(self) -> None:
        if not INSTALL_SCRIPT.exists():
            messagebox.showerror("Instalación", f"No se encuentra el archivo {INSTALL_SCRIPT.name}.")
            return
        if self.running:
            return
        self.mode = "instalar"
        self.progress.configure(value=0, maximum=100)
        self.status_var.set("Instalando dependencias, espera un momento...")
        self.emit("Ejecutando instalar.sh...", "titulo")
        self.refresh_buttons()

        def worker() -> None:
            process = subprocess.Popen(
                ["/bin/bash", str(INSTALL_SCRIPT)],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for raw in process.stdout:
                self.events.put(("line", (raw, None)))
            process.wait()
            self.events.put(("exit", process.returncode))

        threading.Thread(target=worker, daemon=True).start()

    def start(self) -> None:
        if self.running:
            return
        if not venv_python_ok():
            messagebox.showwarning(
                "Entorno no preparado", "Pulsa «Instalar ahora» para preparar el entorno."
            )
            return
        if self.reiniciar_sesion.get() and SESSION_DIR.exists():
            shutil.rmtree(SESSION_DIR, ignore_errors=True)
            self.emit("Sesión de Facebook anterior eliminada.", "warn")
        self.cancelled = False
        self.mode = "descarga"
        self.saved_items = 0
        self.total_items = 0
        self.progress.configure(value=0, maximum=100)
        self.status_var.set("Abriendo Facebook en Chromium...")
        self.emit("Iniciando descargador...", "titulo")
        self.process = subprocess.Popen(
            [str(VENV_PY), "-u", str(MAIN_PY)],
            cwd=str(BASE_DIR),
            env=dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        self.refresh_buttons()

    def _reader(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        for raw in process.stdout:
            self.events.put(("line", (raw, None)))
        process.wait()
        self.events.put(("exit", process.returncode))

    def _signal(self, process: subprocess.Popen, sig: int, group: bool = False) -> None:
        try:
            if group:
                os.killpg(os.getpgid(process.pid), sig)
            else:
                process.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def cancel(self) -> None:
        if not self.running:
            return
        self.cancelled = True
        self.status_var.set("Cancelando...")
        self.cancelar_btn.configure(state="disabled")
        self.emit("Cancelando: cerrando el navegador...", "warn")
        process = self.process
        assert process is not None
        self._signal(process, signal.SIGINT)
        threading.Thread(target=self._force_stop, args=(process,), daemon=True).start()

    def _force_stop(self, process: subprocess.Popen) -> None:
        try:
            process.wait(timeout=GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        self._signal(process, signal.SIGTERM, group=True)
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        self._signal(process, signal.SIGKILL, group=True)

    def kill_leftover_browser(self) -> None:
        if SESSION_DIR.exists():
            subprocess.run(["pkill", "-f", str(SESSION_DIR)], capture_output=True, check=False)

    def handle_exit(self, code: int) -> None:
        self.end_partial()
        self.process = None
        if self.mode == "instalar":
            self.mode = ""
            self.refresh_buttons()
            if code == 0:
                self.status_var.set("Instalación completada. Ya puedes iniciar la descarga.")
                self.emit("Instalación completada.", "ok")
                self.root.after(400, self.check_environment)
            else:
                self.status_var.set("La instalación falló. Revisa el registro.")
                self.emit(f"instalar.sh terminó con código {code}.", "err")
            return

        self.mode = ""
        self.refresh_buttons()
        if self.cancelled:
            self.kill_leftover_browser()
            self.status_var.set("Descarga cancelada.")
            self.emit("Descarga cancelada. El CSV conserva lo ya descargado.", "warn")
        elif code == 0:
            self.status_var.set(f"Listo: {self.saved_items} portadas descargadas.")
            self.emit("Proceso terminado correctamente.", "ok")
            messagebox.showinfo(
                "Descarga terminada",
                f"Se guardaron {self.saved_items} portadas.\n\n"
                f"Imágenes: {OUTPUT_DIR}\nÍndice: {CSV_PATH.name}",
            )
        else:
            self.status_var.set("La descarga terminó con errores.")
            self.emit(f"El descargador terminó con código {code}.", "err")
            messagebox.showerror(
                "Descarga interrumpida",
                "Ocurrió un error. Revisa el registro de actividad para ver el detalle.",
            )

    def reveal(self, path: Path) -> None:
        if not path.exists():
            messagebox.showinfo("Todavía no existe", f"No se encontró:\n{path.name}")
            return
        opener = shutil.which("open") or shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, str(path)])

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askokcancel(
                "Salir", "La descarga está en curso. ¿Cancelarla y cerrar la aplicación?"
            ):
                return
            self.cancel()
            self.root.after(1200, self.root.destroy)
            return
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        MarketplaceApp(root)
    except tk.TclError as exc:
        print(f"No se pudo abrir la ventana gráfica: {exc}", file=sys.stderr)
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())