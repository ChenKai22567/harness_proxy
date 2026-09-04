$ErrorActionPreference = 'Stop'

$AppRoot = $PSScriptRoot
$DesktopPath = [Environment]::GetFolderPath('Desktop')
$ShortcutName = "Harness代理启动.lnk"
$ShortcutPath = Join-Path $DesktopPath $ShortcutName

# Clean up legacy shortcuts if they exist
$OldShortcuts = @(
    "AI 代理启动中心.lnk",
    "AI 代理启动中心 (AI Proxy Hub).lnk"
)
foreach ($old in $OldShortcuts) {
    $p = Join-Path $DesktopPath $old
    if (Test-Path $p) {
        Remove-Item -Path $p -Force -ErrorAction SilentlyContinue
        Write-Host "Removed legacy shortcut: $old" -ForegroundColor Gray
    }
}

$IconPath = Join-Path $AppRoot "assets\icon.ico"
$DistExe = Join-Path $AppRoot "dist\HarnessProxyLauncher\HarnessProxyLauncher.exe"
if (-not (Test-Path $DistExe)) {
    $DistExeFallback = Join-Path $AppRoot "dist\AIProxyHub\AIProxyHub.exe"
    if (Test-Path $DistExeFallback) {
        $DistExe = $DistExeFallback
    }
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)

if (Test-Path $DistExe) {
    $Shortcut.TargetPath = $DistExe
    $Shortcut.WorkingDirectory = Split-Path -Parent $DistExe
    $Shortcut.IconLocation = "$DistExe,0"
    $Shortcut.Description = "Harness代理启动 (Antigravity & Codex 专用代理管理器)"
    Write-Host "Creating desktop shortcut pointing to: $DistExe"
}
else {
    $PythonExe = (Get-Command python.exe).Source
    $PythonwExe = $PythonExe -replace 'python\.exe$', 'pythonw.exe'
    if (-not (Test-Path $PythonwExe)) {
        $PythonwExe = $PythonExe
    }

    $Shortcut.TargetPath = $PythonwExe
    $Shortcut.Arguments = "`"$AppRoot\main.py`""
    $Shortcut.WorkingDirectory = $AppRoot
    if (Test-Path $IconPath) {
        $Shortcut.IconLocation = "$IconPath,0"
    }
    $Shortcut.Description = "Harness代理启动 (Antigravity & Codex 专用代理管理器)"
    Write-Host "Creating desktop shortcut pointing to Python GUI: $ShortcutPath"
}

$Shortcut.Save()
Write-Host "Desktop shortcut created successfully at: $ShortcutPath" -ForegroundColor Green
