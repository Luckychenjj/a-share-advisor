param([switch]$ServerOnly)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$localDirectory = Join-Path $PSScriptRoot '.local-recovery'
New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null
$secretFile = Join-Path $localDirectory 'server-token.txt'
if (!(Test-Path -LiteralPath $secretFile)) {
    $secretValue = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
    Set-Content -LiteralPath $secretFile -Value $secretValue -NoNewline
}
$env:XIAO_A_SERVER_TOKEN = (Get-Content -LiteralPath $secretFile -Raw).Trim()
$pythonExecutable = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($ServerOnly) {
    & $pythonExecutable -m xiao_a.cli cloud --host 0.0.0.0 --port 8766
    exit $LASTEXITCODE
}
$serverArguments = @('-NoProfile', '-File', ('"' + $PSCommandPath + '"'), '-ServerOnly')
$serverProcess = Start-Process -FilePath 'powershell.exe' -ArgumentList $serverArguments -WindowStyle Hidden -PassThru
try {
    $env:XIAO_A_CLOUD_URL = 'http://127.0.0.1:8766'
    $env:XIAO_A_CLOUD_TOKEN = $env:XIAO_A_SERVER_TOKEN
    & $pythonExecutable -m xiao_a.cli dashboard --port 8767 --no-browser
} finally {
    # Stop only this launcher and its child server, preserving other projects.
    & taskkill.exe /PID $serverProcess.Id /T /F | Out-Null
}
