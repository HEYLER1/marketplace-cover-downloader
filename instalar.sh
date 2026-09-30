#!/bin/bash
set -uo pipefail

cd "$(dirname "$0")" || exit 1

pause_on_error() {
  echo ""
  echo "La instalación no pudo completarse. Revisa el mensaje de arriba."
  if [ -t 0 ]; then
    read -r -p "Presiona Enter para cerrar esta ventana..." _
  fi
  exit 1
}

echo "=============================================="
echo " Preparación del descargador de Marketplace"
echo "=============================================="

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "No se encontró Python 3.9 o superior. Instálalo desde https://www.python.org/downloads/mac/"
  pause_on_error
fi

echo "Python encontrado: $($PYTHON_BIN --version)"

if [ ! -x "venv/bin/python" ]; then
  echo "Creando el entorno virtual (venv)..."
  "$PYTHON_BIN" -m venv venv || pause_on_error
else
  echo "El entorno virtual ya existe."
fi

echo "Instalando paquetes de Python (puede tardar varios minutos)..."
./venv/bin/python -m pip install --upgrade pip --quiet || echo "Se conserva la versión actual de pip."
./venv/bin/python -m pip install -r requirements.txt || pause_on_error

echo "Descargando el navegador que usa Playwright..."
./venv/bin/python -m playwright install chromium || pause_on_error

echo ""
echo "Verificando la instalación..."
if ! ./venv/bin/python -c "import playwright, ultralytics, PIL, tkinter" 2>/dev/null; then
  echo "Falta algún componente (revisa el mensaje anterior) o la versión de Python no trae Tkinter."
  pause_on_error
fi

echo ""
echo "=============================================="
echo " Listo. Abre la interfaz con:"
echo "   ./ejecutar.command   (o doble clic en ese archivo)"
echo "=============================================="

if [ -t 0 ]; then
  read -r -p "Presiona Enter para cerrar esta ventana..." _
fi