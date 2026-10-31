# Install the UofT MCP server on Windows. No admin rights needed.
#
#   irm https://raw.githubusercontent.com/SleepyPandas/UofT-MCP/master/install.ps1 | iex
#
# What it does: installs uv (https://docs.astral.sh/uv/) if missing, installs or upgrades
# the `uoft-mcp` tool with it, then prints the MCP client config to paste. Safe to re-run.
#
# Optional environment variables (set them before running):
#   $env:UOFT_MCP_SOURCE        Package to install (default: uoft-mcp from PyPI). Use a git
#                               URL such as git+https://github.com/SleepyPandas/UofT-MCP to
#                               follow the unreleased master branch, or a local checkout path.
#   $env:UOFT_MCP_WITH_BROWSER  Set to 1 to also download Chromium now (needed only to sign in).
#
# Everything lives in one function so that `irm | iex` never exits or changes your session.

function Install-UofTMcp {
    # Native tools write progress to stderr; check $LASTEXITCODE instead of aborting on it.
    $ErrorActionPreference = 'Continue'

    $source = if ($env:UOFT_MCP_SOURCE) { $env:UOFT_MCP_SOURCE } else { 'uoft-mcp' }
    $uvInstallerUrl = 'https://astral.sh/uv/install.ps1'
    $originalPath = $env:Path
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    # uv's installer puts binaries here by default; check it in case PATH was not refreshed.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        Write-Host "Found uv: $($uv.Source)"
    } else {
        Write-Host "uv not found. Installing it from $uvInstallerUrl (user-level, no admin)..."
        Invoke-RestMethod $uvInstallerUrl | Invoke-Expression
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uv) {
            throw 'uv installed but is not on PATH. Open a new terminal and re-run this script.'
        }
    }

    # Large first-time downloads (Python plus dependencies) can exceed uv's default timeout.
    if (-not $env:UV_HTTP_TIMEOUT) { $env:UV_HTTP_TIMEOUT = '300' }

    Write-Host "Installing uoft-mcp from: $source"
    & uv tool install --force --upgrade $source
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool install failed (exit code $LASTEXITCODE)."
    }

    $binDir = (& uv tool dir --bin | Select-Object -First 1)
    $exe = Join-Path $binDir 'uoft-mcp.exe'
    if (-not (Test-Path $exe)) {
        throw "Expected $exe after install, but it does not exist."
    }

    if ($env:UOFT_MCP_WITH_BROWSER -eq '1') {
        Write-Host 'Downloading Chromium for UofT sign-in...'
        & $exe auth setup
    }

    Write-Host ''
    & $exe doctor --command $exe
    if ($LASTEXITCODE -ne 0) {
        throw 'uoft-mcp doctor reported a problem (see above).'
    }

    Write-Host ''
    if (($originalPath -split ';') -notcontains $binDir) {
        Write-Host "Note: $binDir is not on your PATH. Run 'uv tool update-shell' to add it"
        Write-Host '(the config above uses the full path, so MCP clients work either way).'
    }
    Write-Host 'Next: paste the JSON above into your MCP client, restart it, then ask it to'
    Write-Host '"Connect my UofT account". Sign-in needs Chromium; download it once with:'
    Write-Host "  $exe auth setup"
    Write-Host 'Uninstall any time with: uv tool uninstall uoft-mcp'
}

try { Install-UofTMcp } finally { Remove-Item Function:\Install-UofTMcp -ErrorAction SilentlyContinue }
