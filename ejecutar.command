#!/bin/bash
# Lanzador de doble clic: prepara el entorno si hace falta y abre la interfaz gráfica.

cd "$(dirname "$0")" || exit 1

needs_setup() {
  [ ! -x "venv/bin/python" ] && return 0
  venv/bin/python -c "import playwright, ultralytics, PIL, tkinter" >/dev/null 2>&1 || return 0
  return 1
}

if needs_setup; then
  clear
  ./instalar.sh
  needs_setup || true
fi

if needs_setup; then
  echo ""
  echo "No se pudo preparar el entorno. Ejecuta ./instalar.sh y revisa los mensajes."
  read -r -p "Presiona Enter para cerrar esta ventana..." _
  exit 1
fi

exec ./venv/bin/python interfaz.py