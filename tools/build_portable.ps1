$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Version = (& $Python -c "import config; print(config.APP_VERSION)").Trim()
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
    Copy-Item -LiteralPath "README.md","Changelog.txt","LICENSE","THIRD_PARTY_NOTICES.md" -Destination $Target -Force
    Copy-Item -LiteralPath "vendor\LICENSE.wechatauto-replica" -Destination $Target -Force
    foreach ($PrivateName in @("data", "reports", "exports", ".secrets")) {
        $PrivatePath = Join-Path $Target $PrivateName
        if (Test-Path -LiteralPath $PrivatePath) {
            Remove-Item -LiteralPath $PrivatePath -Recurse -Force
        }
    }
    $PrivateFiles = Get-ChildItem -LiteralPath $Target -Recurse -File | Where-Object {
        $_.Name -match '(settings\.json|secrets\.bin|\.db(?:-.*)?$|\.pem$|\.key$)'
    }
    if ($PrivateFiles) {
        throw "发布目录包含用户数据或密钥，已停止打包: $($PrivateFiles.FullName -join ', ')"
    }
    $Zip = Join-Path $ProjectRoot "dist\$Name-v$Version-windows-x64.zip"
    if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
    Compress-Archive -Path "$Target\*" -DestinationPath $Zip -CompressionLevel Optimal
    Write-Host "构建完成: $Zip"
} finally {
    Pop-Location
}
