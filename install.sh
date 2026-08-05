#!/bin/sh
# Webbee installer — bootstraps uv (which brings its own Python) then installs webbee.
set -e

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python toolchain manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin or ~/.cargo/bin; make it visible for this run.
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

echo "Installing webbee…"
# The code index (tree-sitter) and its semantic arm (model2vec) are BASE
# dependencies since 0.3.51 — no extras needed. The [intel,intel-embed] names
# still exist as empty aliases, so older copies of this script keep working.
uv tool install "webbee"

echo ""
echo "✅ webbee installed. Start it with:  webbee"
echo "   (if 'webbee' is not found, add uv's tool bin to your PATH: uv tool update-shell)"
