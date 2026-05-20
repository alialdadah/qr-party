# Load KEY=VALUE pairs from .env into the current PowerShell session.
# Usage: .\load-env.ps1
foreach ($line in Get-Content .env) {
    if ($line -match '^\s*([^#=][^=]*)=(.*)$') {
        $key = $matches[1].Trim()
        $val = $matches[2].Trim()
        Set-Item "env:$key" $val
        Write-Host "  set $key"
    }
}
