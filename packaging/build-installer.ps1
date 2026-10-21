param(
    [string]$ToolsRoot = $(if ($env:XIAO_A_TOOLS_ROOT) { $env:XIAO_A_TOOLS_ROOT } else { Join-Path $env:LOCALAPPDATA 'XiaoAAdvisorTools' }),
    [string]$Compiler = $(if ($env:XIAO_A_ISCC) { $env:XIAO_A_ISCC } else { Join-Path $ToolsRoot 'InnoSetup\ISCC.exe' }),
    [string]$OutputFolder = 'Installer'
)
$ErrorActionPreference = 'Stop'
$appsRoot = [IO.Path]::GetFullPath((Join-Path $ToolsRoot 'Apps'))
$buildsRoot = [IO.Path]::GetFullPath((Join-Path $ToolsRoot 'Builds'))
$stage = [IO.Path]::GetFullPath((Join-Path $buildsRoot 'InstallerStage'))
$output = [IO.Path]::GetFullPath((Join-Path $buildsRoot $OutputFolder))
if (!$output.StartsWith($buildsRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write installer outside Builds: $output"
}
if (!(Test-Path -LiteralPath $Compiler -PathType Leaf)) { throw "Inno Setup compiler not found: $Compiler" }
$source = Get-ChildItem -LiteralPath $appsRoot -Directory |
    Where-Object { $_.Name -match '^XiaoAAdvisor-\d{8}-\d{6}$' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (!$source) { throw "No packaged desktop build found in $appsRoot" }
if (!(Test-Path -LiteralPath (Join-Path $source.FullName 'xiao_a_client.exe')) -or
    !(Test-Path -LiteralPath (Join-Path $source.FullName 'backend\xiao_a_service.exe'))) {
    throw "Source package is incomplete: $($source.FullName)"
}
if (Test-Path -LiteralPath $stage) {
    if (!$stage.StartsWith($buildsRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean staging path outside Builds: $stage"
    }
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$null = & robocopy $source.FullName $stage /E /XJ /R:1 /W:1 /NFL /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { throw "Could not stage application files (robocopy $LASTEXITCODE)" }
# Do not ship the build machine's local paths or its account data.
'{"portfolio_file":""}' | Set-Content -LiteralPath (Join-Path $stage 'desktop-config.json') -Encoding utf8
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'app_icon.ico') -Destination (Join-Path $stage 'app_icon.ico') -Force
New-Item -ItemType Directory -Path $output -Force | Out-Null
$iss = Join-Path $PSScriptRoot 'installer.iss'
$appVersion = ((Get-Item -LiteralPath (Join-Path $stage 'xiao_a_client.exe')).VersionInfo.ProductVersion -split '\+')[0]
if ($appVersion -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid application version: $appVersion" }
& $Compiler "/DAppSource=$stage" "/DOutputDir=$output" "/DAppVersion=$appVersion" $iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed ($LASTEXITCODE)" }
$readme = @'
小A顾问 1.0（Windows 64 位）

安装：双击“小A顾问1.0.exe”，按向导完成安装。安装到 Program Files 等受保护目录时会请求管理员权限（Windows 用户账户控制）。
数据：首次启动会在当前用户的 LocalAppData\XiaoAAdvisor 中创建独立的空账户文件；不会包含打包电脑的持仓或账户信息。卸载时保留账户数据。
联网：行情、历史K线、资金流、公告等外部数据需要网络和数据源可用。模型评分与账户风控在本机运行，不需要启动 GPT 或配置模型 API Key。
运行：通过开始菜单或安装时选择的桌面快捷方式启动。程序包含客户端、本地分析服务及所需运行库；安装电脑无需另装 Python。
系统：Windows 10/11 64 位。
'@
$readme = $readme.Replace('1.0', $appVersion)
$readme | Set-Content -LiteralPath (Join-Path $output '安装说明.txt') -Encoding utf8
Write-Output "Installer: $(Join-Path $output "小A顾问$appVersion.exe")"
Write-Output "Source desktop package: $($source.FullName)"
