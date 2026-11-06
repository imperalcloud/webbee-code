#!/bin/sh
# Webbee installer — bootstraps uv (which brings its own Python) then installs webbee.
# Always the newest release, straight from git: webbee, imperal-mcp and imperal-sdk from their repositories'
# `stable` branch, which every release moves to its tag. uv records the branch, never a version, so running this
# again, or `uv tool upgrade webbee`, brings the newest release. No git on the machine: the newest from PyPI.
set -e

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (Python toolchain manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin or ~/.cargo/bin; make it visible for this run.
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

GH="https://github.com/imperalcloud"
if command -v git >/dev/null 2>&1; then
  echo "Installing webbee (newest release, from git)…"
  uv tool install --force --refresh \
    --with "imperal-mcp @ git+$GH/imperal-mcp.git@stable" \
    --with "imperal-sdk[universal-index] @ git+$GH/imperal-sdk.git@stable" \
    "webbee @ git+$GH/webbee-code.git@stable"
else
  echo "Installing webbee (newest release, from PyPI: no git here)…"
  uv tool install --force --refresh "webbee"
fi

echo ""
echo "✅ webbee installed. Start it with:  webbee"
echo "   (if 'webbee' is not found, add uv's tool bin to your PATH: uv tool update-shell)"
