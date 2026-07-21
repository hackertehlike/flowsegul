#!/usr/bin/env bash
# flowsegul installer — puts the `flowsegul` command on your PATH, and (optionally)
# links the Claude Code skill so you can invoke it from chat.
#
#   ./install.sh          # install the CLI only
#   ./install.sh --skill  # also link the Claude Code skill into ~/.claude/skills
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="${HOME}/.local/bin"

# --- CLI ---
mkdir -p "$bin_dir"
ln -sf "$repo/scripts/flowsegul" "$bin_dir/flowsegul"
echo "✓ linked $bin_dir/flowsegul → scripts/flowsegul"

case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "⚠  $bin_dir is not on your PATH. Add this to your shell rc:"
     echo "     export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# --- Claude Code skill (optional) ---
if [ "${1:-}" = "--skill" ]; then
  skills_dir="${HOME}/.claude/skills"
  mkdir -p "$skills_dir"
  ln -sfn "$repo" "$skills_dir/flowsegul"
  echo "✓ linked $skills_dir/flowsegul → $repo (Claude Code skill)"
fi

# --- sanity check ---
if command -v python3 >/dev/null 2>&1; then
  echo "✓ python3 found: $(python3 --version)"
else
  echo "⚠  python3 not found — flowsegul needs Python 3.6+"
fi

echo "Done. Try:  flowsegul --changed   (inside a Python/FastAPI repo)"
