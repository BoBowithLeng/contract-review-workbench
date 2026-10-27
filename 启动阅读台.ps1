$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw '请先按 README 创建 .venv 并安装 requirements.txt。'
}
Write-Host '合同审查工作台：http://127.0.0.1:8519/'
& $Python (Join-Path $PSScriptRoot 'reader_server.py') --port 8519
