$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Version = "0.1.0"
$Name = "WeChatAISummary"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "未找到 .venv，请先安装 requirements-dev.txt"
}

Push-Location $ProjectRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --onedir --windowed `
        --name $Name `
        --add-data "gui\static;gui\static" `
        --hidden-import winotify `
        --collect-all winotify `
        gui_app.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

    $Target = Join-Path $ProjectRoot "dist\$Name"
    Copy-Item -LiteralPath "README.md","LICENSE","THIRD_PARTY_NOTICES.md" -Destination $Target -Force
    Copy-Item -LiteralPath "vendor\LICENSE.wechatauto-replica" -Destination $Target -Force
    $Zip = Join-Path $ProjectRoot "dist\$Name-v$Version-windows-x64.zip"
    if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
    Compress-Archive -Path "$Target\*" -DestinationPath $Zip -CompressionLevel Optimal
    Write-Host "构建完成: $Zip"
} finally {
    Pop-Location
}
