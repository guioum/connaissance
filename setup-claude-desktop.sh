#!/usr/bin/env bash
# Setup the connaissance MCP server in Claude Desktop configuration.
#
# Usage:
#   ./setup-claude-desktop.sh
#
# This script:
# - Finds the installed connaissance CLI (~/.local/bin/connaissance or $PATH)
# - Finds the MCP server index.js relative to this script
# - Adds/updates the "connaissance" entry in Claude Desktop's config
# - Preserves all existing config (other MCP servers, preferences, etc.)

set -euo pipefail

CONFIG_FILE="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_JS="$SCRIPT_DIR/mcpb/server/index.js"

if [ ! -f "$SERVER_JS" ]; then
  echo "Error: MCP server not found at $SERVER_JS"
  echo "Run: cd mcpb/server && npm install"
  exit 1
fi

# Verify CLI is installed
if ! command -v connaissance >/dev/null 2>&1 && [ ! -f "$HOME/.local/bin/connaissance" ]; then
  echo "Error: connaissance CLI not found."
  echo ""
  echo "Install it first:"
  echo "  uv tool install git+https://github.com/guioum/connaissance"
  echo "  # or"
  echo "  pip install --user git+https://github.com/guioum/connaissance"
  exit 1
fi

CLI_PATH="${HOME}/.local/bin/connaissance"
if [ ! -f "$CLI_PATH" ]; then
  CLI_PATH="$(command -v connaissance)"
fi

python3 -c "
import json, os, sys

config_file = sys.argv[1]
server_js = sys.argv[2]
cli_path = sys.argv[3]

if os.path.exists(config_file):
    with open(config_file) as f:
        config = json.load(f)
else:
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    config = {}

config.setdefault('mcpServers', {})['connaissance'] = {
    'command': 'node',
    'args': [server_js],
    'env': {'CONNAISSANCE_CLI': cli_path},
}

with open(config_file, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')

print(f'Done. Config written to {config_file}')
print(f'  Server: {server_js}')
print(f'  CLI: {cli_path}')
" "$CONFIG_FILE" "$SERVER_JS" "$CLI_PATH"

echo ""
echo "Restart Claude Desktop to apply changes."
