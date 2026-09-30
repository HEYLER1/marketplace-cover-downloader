#!/bin/bash
# Genera las capturas de la interfaz (docs/capturas) para el README.

cd "$(dirname "$0")" || exit 1

if [ ! -x "venv/bin/python" ]; then
  echo "Primero hay que instalar el entorno: ./instalar.sh"
  read -r -p "Presiona Enter para cerrar..." _
  exit 1
fi

./venv/bin/python capturar.py
echo ""
read -r -p "Presiona Enter para cerrar esta ventana..." _
