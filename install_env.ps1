<#
核心职责：Flow2API 新环境一键初始化与重装脚本（Windows PowerShell）。
主要功能：
1. 自动创建 Python 虚拟环境 (venv) 并升级 pip；
2. 安装项目依赖 (requirements.txt)、Playwright 及 CloakBrowser；
3. 自动执行 setup_cloakbrowser.py 打好直连高速下载补丁、写入 License Key 并完成 CloakBrowser Pro 内核安装。
#>

param(
    [string]$LicenseKey = $env:CLOAKBROWSER_LICENSE_KEY,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "  Flow2API + CloakBrowser 新环境一键部署脚本" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan

# 1. 创建虚拟环境
if (-not (Test-Path "$ProjectRoot\venv\Scripts\python.exe")) {
    Write-Host "[1/4] 正在创建 Python 虚拟环境 venv ..." -ForegroundColor Yellow
    & $PythonExe -m venv "$ProjectRoot\venv"
} else {
    Write-Host "[1/4] 已检测到现有虚拟环境 venv" -ForegroundColor Green
}

$VenvPython = "$ProjectRoot\venv\Scripts\python.exe"

# 2. 安装 Python 依赖
Write-Host "[2/4] 正在安装 requirements.txt 与 cloakbrowser ..." -ForegroundColor Yellow
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r "$ProjectRoot\requirements.txt"
& $VenvPython -m pip install cloakbrowser playwright

# 3. 应用 CloakBrowser 直连下载补丁并下载 Pro 内核
Write-Host "[3/4] 正在配置 CloakBrowser 直连加速补丁并下载内核 ..." -ForegroundColor Yellow
& $VenvPython "$ProjectRoot\setup_cloakbrowser.py" --key "$LicenseKey"

# 4. 验证安装结果
Write-Host "[4/4] 正在验证 CloakBrowser 内核与数据库配置 ..." -ForegroundColor Yellow
& $VenvPython -c "import cloakbrowser; print('CloakBrowser binary:', cloakbrowser.binary_info())"

Write-Host "=====================================================" -ForegroundColor Green
Write-Host "  部署完成！启动服务命令：" -ForegroundColor Green
Write-Host "  .\venv\Scripts\python.exe main.py" -ForegroundColor White
Write-Host "=====================================================" -ForegroundColor Green

