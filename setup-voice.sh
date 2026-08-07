#!/bin/bash
# Sets up voice input (whisper.cpp), fully local. Optional: if you don't need
# voice input, you don't have to run this script - the rest of the dashboard
# works the same without it.
set -euo pipefail
cd "$(dirname "$0")"

echo "== cmake check =="
if command -v cmake >/dev/null 2>&1; then
  CMAKE=cmake
elif sudo -n true 2>/dev/null; then
  echo "installing cmake with sudo..."
  sudo apt-get update -qq && sudo apt-get install -y cmake
  CMAKE=cmake
else
  echo "no sudo available - installing cmake into an isolated venv (won't pollute the system)"
  python3 -m venv .buildenv
  .buildenv/bin/pip install --quiet cmake
  CMAKE="$(pwd)/.buildenv/bin/cmake"
fi
echo "cmake: $($CMAKE --version | head -1)"

echo "== cloning whisper.cpp =="
if [ ! -d whisper.cpp ]; then
  git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git
fi

echo "== building (memory-conscious, fewer parallel jobs) =="
cd whisper.cpp
"$CMAKE" -B build -DCMAKE_BUILD_TYPE=Release
"$CMAKE" --build build --config Release -j 2

echo "== downloading the model (~141MB, one time) =="
bash models/download-ggml-model.sh base

echo
echo "✅ Voice input is ready. Run python3 fleet.py and 🎤 will work in the dashboard."
