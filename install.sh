#!/bin/sh
# Install the UofT MCP server on macOS or Linux. No admin rights needed.
#
#   curl -LsSf https://raw.githubusercontent.com/SleepyPandas/UofT-MCP/master/install.sh | sh
#
# What it does: installs uv (https://docs.astral.sh/uv/) if missing, installs or upgrades
# the `uoft-mcp` tool with it, then prints the MCP client config to paste. Safe to re-run.
#
# Optional environment variables:
#   UOFT_MCP_SOURCE        Package to install (default: uoft-mcp from PyPI). Use a git URL,
#                          for example git+https://github.com/SleepyPandas/UofT-MCP, to
#                          follow the unreleased master branch, or a local checkout path.
#   UOFT_MCP_WITH_BROWSER  Set to 1 to also download Chromium (~150 MB). Not needed when
#                          Google Chrome or Microsoft Edge is installed; sign-in uses them.
set -eu

SOURCE="${UOFT_MCP_SOURCE:-uoft-mcp}"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# uv's installer puts binaries here by default; check it in case PATH was not refreshed.
ORIGINAL_PATH="$PATH"
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export PATH

if command -v uv >/dev/null 2>&1; then
    say "Found uv: $(command -v uv)"
else
    say "uv not found. Installing it from $UV_INSTALLER_URL (user-level, no sudo)..."
    command -v curl >/dev/null 2>&1 || die "curl is required to install uv."
    curl -LsSf "$UV_INSTALLER_URL" | sh
    command -v uv >/dev/null 2>&1 || die "uv installed but is not on PATH. Open a new terminal and re-run this script."
fi

# Large first-time downloads (Python plus dependencies) can exceed uv's default timeout.
UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-300}"
export UV_HTTP_TIMEOUT

say "Installing uoft-mcp from: $SOURCE"
uv tool install --force --upgrade "$SOURCE"

BIN_DIR="$(uv tool dir --bin)"
EXE="$BIN_DIR/uoft-mcp"
[ -x "$EXE" ] || die "Expected $EXE after install, but it does not exist."

if [ "${UOFT_MCP_WITH_BROWSER:-0}" = "1" ]; then
    say "Downloading Chromium for UofT sign-in..."
    UOFT_MCP_BROWSER=chromium "$EXE" auth setup || die "Chromium download failed (see above)."
fi

say ""
"$EXE" doctor --command "$EXE" || die "uoft-mcp doctor reported a problem (see above)."

say ""
case ":$ORIGINAL_PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say "Note: $BIN_DIR is not on your PATH. Run 'uv tool update-shell' to add it" \
           "(the config above uses the full path, so MCP clients work either way)." ;;
esac
say "Next: paste the JSON above into your MCP client, restart it, then ask it to"
say "\"Connect my UofT account\". Sign-in opens Google Chrome or Microsoft Edge, so no"
say "browser download is needed. If the check above found neither, run:"
say "  ${EXE} auth setup"
say "Uninstall any time with: uv tool uninstall uoft-mcp"
