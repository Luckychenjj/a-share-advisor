param([string]$ToolsRoot = $(if ($env:XIAO_A_TOOLS_ROOT) { $env:XIAO_A_TOOLS_ROOT } else { Join-Path $env:LOCALAPPDATA 'XiaoAAdvisorTools' }))
# Run in a visible administrator PowerShell window to inspect live build output.
$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'Xiao A Advisor - Desktop Build'
$repository = Split-Path $PSScriptRoot
$logs = Join-Path $ToolsRoot 'downloads'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$log = Join-Path $logs 'desktop-package.log'
$status = Join-Path $logs 'desktop-package-status.txt'
# Optional: set XIAO_A_GIT_DIR to a portable Git 'cmd' folder if git is not on PATH.
if ($env:XIAO_A_GIT_DIR -and (Test-Path -LiteralPath $env:XIAO_A_GIT_DIR)) { $env:Path = $env:XIAO_A_GIT_DIR + ';' + $env:Path }
$env:PUB_HOSTED_URL = 'https://pub.flutter-io.cn'
$env:FLUTTER_STORAGE_BASE_URL = 'https://storage.flutter-io.cn'
$buildExe = Join-Path $ToolsRoot 'Builds\XiaoAClient\build\windows\x64\runner\Release\xiao_a_client.exe'
Get-Process xiao_a_client -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $buildExe } | Stop-Process
'Building desktop package' | Set-Content -LiteralPath $status
Write-Host "Source: $repository"
Write-Host "Live log: $log"
Write-Host 'Building Python backend and Flutter Windows application...'
try {
    # Windows PowerShell represents native stderr progress as error records.
    # Build-desktop checks each native exit code and throws on actual failure.
    $ErrorActionPreference = 'Continue'
    & (Join-Path $PSScriptRoot 'build-desktop.ps1') -ToolsRoot $ToolsRoot *>&1 |
        Tee-Object -FilePath $log
    'Completed' | Set-Content -LiteralPath $status
    Write-Host 'BUILD COMPLETED. You can keep this window open to inspect the output.' -ForegroundColor Green
} catch {
    'Failed' | Set-Content -LiteralPath $status
    $_ | Out-String | Add-Content -LiteralPath $log
    Write-Host "BUILD FAILED: $_" -ForegroundColor Red
} finally {
    Set-Location $repository
}
