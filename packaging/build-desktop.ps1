param(
    [string]$ToolsRoot = $(if ($env:XIAO_A_TOOLS_ROOT) { $env:XIAO_A_TOOLS_ROOT } else { Join-Path $env:LOCALAPPDATA 'XiaoAAdvisorTools' }),
    [string]$PortfolioFile = ''
)
$ErrorActionPreference = 'Stop'
$repository = Split-Path $PSScriptRoot
# Prefer this checkout after project migration; an old editable environment
# may still reference the previous location.
$env:PYTHONPATH = Join-Path $repository 'src'
$pubspec = Get-Content -LiteralPath (Join-Path $repository 'client\pubspec.yaml') -Raw
if ($pubspec -notmatch '(?m)^version:\s*(\d+\.\d+\.\d+)\+(\d+)\s*$') { throw 'Could not read app version from client/pubspec.yaml' }
$appVersion = $Matches[1]
$buildNumber = $Matches[2]
if (!$PortfolioFile) { $PortfolioFile = Join-Path $repository 'data\portfolio.json' }
$PortfolioFile = (Resolve-Path -LiteralPath $PortfolioFile).Path
$userDataRoot = Join-Path $ToolsRoot 'UserData'
$userPortfolioFile = Join-Path $userDataRoot 'portfolio.json'
New-Item -ItemType Directory -Path $userDataRoot -Force | Out-Null
if (!(Test-Path -LiteralPath $userPortfolioFile)) {
    Copy-Item -LiteralPath $PortfolioFile -Destination $userPortfolioFile
}
$flutter = Join-Path $ToolsRoot 'Flutter\bin\flutter.bat'
$python = Join-Path $repository '.venv\Scripts\python.exe'
$clientCopy = Join-Path $ToolsRoot 'Builds\XiaoAClient'
$backendDist = Join-Path $ToolsRoot 'Builds\BackendDist'
$release = Join-Path $ToolsRoot ('Apps\XiaoAAdvisor-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$env:PUB_CACHE = Join-Path $ToolsRoot 'PubCache'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $ToolsRoot 'PyInstallerCache'
New-Item -ItemType Directory -Path $release -Force | Out-Null
Set-Location $repository
$ErrorActionPreference = 'Continue'
& $python -m PyInstaller --noconfirm --onedir --noconsole --name xiao_a_service --distpath $backendDist --workpath (Join-Path $ToolsRoot 'Builds\BackendWork') --specpath (Join-Path $ToolsRoot 'Builds\BackendSpecs') --paths src --collect-data xiao_a.web --collect-submodules xiao_a packaging\desktop_service.py
if ($LASTEXITCODE -ne 0) { throw 'Backend build failed' }
& robocopy (Join-Path $repository 'client') $clientCopy /E /XJ /XD build .dart_tool ephemeral .gradle /XF local.properties .flutter-plugins-dependencies /R:1 /W:1 /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { throw 'Client copy failed' }
Set-Location $clientCopy
& $flutter pub get
if ($LASTEXITCODE -ne 0) { throw 'Client dependencies failed' }
& $flutter build windows --release --no-pub --no-tree-shake-icons --build-name $appVersion --build-number $buildNumber "--dart-define=APP_VERSION=$appVersion"
if ($LASTEXITCODE -ne 0) { throw 'Windows build failed' }
& robocopy (Join-Path $clientCopy 'build\windows\x64\runner\Release') $release /E /R:1 /W:1 /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { throw 'Release copy failed' }
& robocopy (Join-Path $backendDist 'xiao_a_service') (Join-Path $release 'backend') /E /R:1 /W:1 /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { throw 'Backend copy failed' }
@{ portfolio_file = $userPortfolioFile } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $release 'desktop-config.json') -Encoding utf8
$ErrorActionPreference = 'Stop'
Copy-Item -LiteralPath (Join-Path $repository 'THIRD_PARTY_NOTICES.md') -Destination $release
Copy-Item -LiteralPath (Join-Path $repository 'client\licenses') -Destination (Join-Path $release 'licenses') -Recurse
$redist = Get-ChildItem -Path (Join-Path $ToolsRoot 'VSBuildTools\VC\Redist\MSVC\*\x64\Microsoft.VC143.CRT') -Directory | Select-Object -First 1
if ($redist) { Copy-Item -Path (Join-Path $redist.FullName '*.dll') -Destination $release }
Write-Output "Desktop directory: $release"

$desktopPath = [Environment]::GetFolderPath('Desktop')
$shortcutName = -join @([char]0x5C0F, 'A', [char]0x987E, [char]0x95EE, '.lnk')
$shortcutPath = Join-Path $desktopPath $shortcutName
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $release 'xiao_a_client.exe'
$shortcut.Arguments = ''
$shortcut.WorkingDirectory = $release
$shortcut.IconLocation = Join-Path $release 'xiao_a_client.exe'
$shortcut.Description = 'Xiao A Advisor desktop app'
$shortcut.Save()
Write-Output "Desktop shortcut: $shortcutPath"

# Keep only the three newest timestamped application packages after a successful build.
$appsRoot = [System.IO.Path]::GetFullPath((Join-Path $ToolsRoot 'Apps'))
$versionDirs = @(Get-ChildItem -LiteralPath $appsRoot -Directory |
    Where-Object { $_.Name -match '^XiaoAAdvisor-\d{8}-\d{6}$' } |
    Sort-Object LastWriteTime -Descending)
$obsoleteDirs = @($versionDirs | Select-Object -Skip 3)
$runningImagePaths = @()
try {
    $runningImagePaths = @(Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object { $_.Name -in @('xiao_a_client.exe', 'xiao_a_service.exe') -and $_.ExecutablePath } |
        ForEach-Object { [System.IO.Path]::GetFullPath($_.ExecutablePath) })
} catch {
    Write-Warning "Could not check running app processes; old package folders will be retained for safety. $_"
}
foreach ($oldDir in $obsoleteDirs) {
    $fullPath = [System.IO.Path]::GetFullPath($oldDir.FullName)
    if (!$fullPath.StartsWith($appsRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the Apps directory: $fullPath"
    }
    if ($runningImagePaths | Where-Object { $_.StartsWith($fullPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) }) {
        Write-Warning "Keeping an older package because it is still running: $fullPath"
        continue
    }
    Remove-Item -LiteralPath $fullPath -Recurse -Force
    Write-Output "Removed older package: $fullPath"
}
