# 强制 PowerShell 使用 UTF-8 编码，防止中文乱码
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 定义导出文本文件名
$outFile = "docker_run_commands.txt"
Remove-Item $outFile -ErrorAction SilentlyContinue

"==================================================" | Out-File $outFile -Encoding utf8
" Docker 容器启动命令与盘符路径备份 (生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))" | Out-File $outFile -Append -Encoding utf8
"==================================================" | Out-File $outFile -Append -Encoding utf8
"" | Out-File $outFile -Append -Encoding utf8

$containers = docker ps -a --format "{{.Names}}"

foreach ($name in $containers) {
    if (-not $name) { continue }
    Write-Host "正在分析容器: $name ..." -ForegroundColor Cyan
    
    "# --------------------------------------------------" | Out-File $outFile -Append -Encoding utf8
    "# 容器名称: $name" | Out-File $outFile -Append -Encoding utf8
    
    # 使用 ConvertFrom-Json 解析
    $inspectJson = docker inspect $name 2>$null | ConvertFrom-Json
    $hostDir = $null
    
    if ($inspectJson -and $inspectJson.Mounts) {
        foreach ($m in $inspectJson.Mounts) {
            $src = $m.Source
            if (-not $src -or $src -like "*docker.sock*" -or $src -like "*docker_engine*") { continue }
            
            # 转换 WSL2 / Docker Desktop 磁盘路径 (/host_mnt/e/grok2api -> E:\grok2api)
            if ($src -match "^/host_mnt/([a-zA-Z])/(.*)") {
                $src = "$($Matches[1].ToUpper()):\" + ($Matches[2] -replace '/','\')
            }
            
            if ($src -match "^[a-zA-Z]:") {
                if ($src -match "\.[a-zA-Z0-9]+$") {
                    $src = Split-Path $src -Parent
                }
                $hostDir = $src
                break
            }
        }
    }
    
    # 写入切换目录命令 cd "X:\路径"
    if ($hostDir) {
        "cd ""$hostDir""" | Out-File $outFile -Append -Encoding utf8
    } else {
        "# (提示: 该容器未挂载本地盘符，使用默认 Docker 内部存储)" | Out-File $outFile -Append -Encoding utf8
    }
    
    # 提取完整的 docker run 命令
    $cmd = docker run --rm -v /var/run/docker.sock:/var/run/docker.sock assaflavie/runlike $name 2>$null
    if ($cmd) {
        # 1. 过滤带引号的 compose / desktop 内部无用系统标签 (--label='com.docker.compose...')
        $cmd = $cmd -replace '\s+--label=["''][^"'']*(com\.docker\.compose|desktop\.docker\.io)[^"'']*["'']', ''

        # 2. 过滤不带引号的 compose / desktop 内部无用系统标签 (--label=com.docker.compose...)
        $cmd = $cmd -replace '\s+--label=(com\.docker\.compose|desktop\.docker\.io)[^\s]*', ''

        # 3. 过滤自动生成的 12 位十六进制随机主机名 (如 --hostname=6b6476325301)
        $cmd = $cmd -replace '\s+--hostname=[0-9a-fA-F]{12}', ''

        # 4. 过滤冗余的默认运行时参数 (--runtime=runc)
        $cmd = $cmd -replace '\s+--runtime=runc', ''

        # 5. 确保包含后台运行参数 -d
        if ($cmd -notmatch '\s+-d(\s+|$)') {
            $cmd = $cmd -replace '^docker run\s+', 'docker run -d '
        }

        $cmd | Out-File $outFile -Append -Encoding utf8
    } else {
        $image = if ($inspectJson) { $inspectJson.Config.Image } else { docker inspect --format '{{.Config.Image}}' $name }
        "docker run -d --name $name $image" | Out-File $outFile -Append -Encoding utf8
    }
    "" | Out-File $outFile -Append -Encoding utf8
}

Write-Host "`n导出完成！已保存至: $outFile (中文正常无乱码)" -ForegroundColor Green
