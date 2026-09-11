# Переименование проекта 1c-odata-gateway -> odata1c-gate с сохранением сессий Claude Code.
#
# ЗАПУСКАТЬ ТОЛЬКО когда закрыты ВСЕ сессии Claude Code по этому проекту и остановлен демон:
#   uv run --directory H:\.GitHub\1c-odata-gateway odata1c daemon stop   (или просто закрыть терминалы)
# Скрипт не трогает содержимое репозитория — только имя каталога и указатели среды.
#
# Что переносится:
#   1. каталог репозитория                H:\.GitHub\1c-odata-gateway -> H:\.GitHub\odata1c-gate
#   2. история сессий и память Claude Code ~\.claude\projects\h---GitHub-1c-odata-gateway -> ...odata1c-gate
#   3. настройки проекта в ~\.claude.json  (доверие каталогу, разрешения инструментов)
#   4. путь к шлюзу в MCP-сервере odata1c  (--directory)

$ErrorActionPreference = 'Stop'

$СтарыйПуть = 'H:\.GitHub\1c-odata-gateway'
$НовыйПуть  = 'H:\.GitHub\odata1c-gate'
$СтарыйКлюч = 'h---GitHub-1c-odata-gateway'
$НовыйКлюч  = 'h---GitHub-odata1c-gate'
$Проекты    = Join-Path $env:USERPROFILE '.claude\projects'
$КонфигJson = Join-Path $env:USERPROFILE '.claude.json'

Write-Host '== 1. Проверки перед началом ==' -ForegroundColor Cyan
if (-not (Test-Path $СтарыйПуть)) { throw "Каталог $СтарыйПуть не найден — возможно, переименование уже сделано" }
if (Test-Path $НовыйПуть) { throw "Каталог $НовыйПуть уже существует — разберитесь вручную, чтобы ничего не затереть" }

$демон = Test-Path '\\.\pipe\anonymous'  # заглушка; реальную проверку делаем по порту
$порт = Test-NetConnection -ComputerName 127.0.0.1 -Port 7171 -InformationLevel Quiet -WarningAction SilentlyContinue
if ($порт) { throw 'Демон шлюза слушает порт 7171. Остановите его (закройте сессии Claude Code) и повторите' }

$занят = Get-Process | Where-Object { $_.Path -like "$СтарыйПуть*" }
if ($занят) { throw "Каталог занят процессами: $($занят.Name -join ', ')" }

Write-Host '== 2. Снятие рабочих копий агентов ==' -ForegroundColor Cyan
# Пути worktree абсолютные: после переименования они сломаются. Рабочие копии — временные,
# их содержимое уже перенесено в ветку m0-probes.
Push-Location $СтарыйПуть
try {
    $копии = git worktree list --porcelain | Select-String '^worktree (.+)$' | ForEach-Object { $_.Matches[0].Groups[1].Value }
    foreach ($копия in $копии) {
        if ($копия -notlike '*\.claude\worktrees\*' -and $копия -notlike '*/.claude/worktrees/*') { continue }
        Write-Host "  снимаю $копия"
        git worktree remove --force $копия 2>&1 | Out-Null
    }
    git worktree prune
    $остались = (git worktree list | Measure-Object -Line).Lines
    Write-Host "  осталось записей worktree: $остались (ожидается 1 — сам репозиторий)"
} finally { Pop-Location }

Write-Host '== 3. Переименование каталога репозитория ==' -ForegroundColor Cyan
Rename-Item -Path $СтарыйПуть -NewName (Split-Path $НовыйПуть -Leaf)
Write-Host "  $СтарыйПуть -> $НовыйПуть"

Write-Host '== 4. Перенос истории сессий и памяти ==' -ForegroundColor Cyan
$откуда = Join-Path $Проекты $СтарыйКлюч
$куда   = Join-Path $Проекты $НовыйКлюч
if (Test-Path $откуда) {
    if (Test-Path $куда) {
        Write-Host "  целевой каталог уже есть — переношу содержимое внутрь"
        Get-ChildItem $откуда -Force | Move-Item -Destination $куда -Force
        Remove-Item $откуда -Force -Recurse
    } else {
        Rename-Item -Path $откуда -NewName $НовыйКлюч
    }
    $сессий = (Get-ChildItem $куда -Filter *.jsonl -ErrorAction SilentlyContinue | Measure-Object).Count
    Write-Host "  перенесено сессий: $сессий, память: $(Test-Path (Join-Path $куда 'memory'))"
} else { Write-Host '  каталог сессий не найден — пропускаю' }

Write-Host '== 5. Настройки проекта в .claude.json ==' -ForegroundColor Cyan
# Ключи там встречаются в обоих регистрах диска ("H:/..." и "h:/..."), переносим все варианты.
$json = Get-Content $КонфигJson -Raw -Encoding UTF8 | ConvertFrom-Json
$изменено = $false
foreach ($ключ in @($json.projects.PSObject.Properties.Name)) {
    if ($ключ -replace '\\','/' -notlike '*/.GitHub/1c-odata-gateway') { continue }
    $новый = ($ключ -replace '1c-odata-gateway','odata1c-gate')
    if (-not $json.projects.PSObject.Properties[$новый]) {
        $json.projects | Add-Member -NotePropertyName $новый -NotePropertyValue $json.projects.$ключ
        Write-Host "  скопированы настройки: $ключ -> $новый"
        $изменено = $true
    }
}
if ($изменено) {
    Copy-Item $КонфигJson "$КонфигJson.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    $json | ConvertTo-Json -Depth 100 | Set-Content $КонфигJson -Encoding UTF8
    Write-Host '  .claude.json обновлён (резервная копия рядом)'
} else { Write-Host '  настройки уже на месте' }

Write-Host '== 6. MCP-сервер odata1c ==' -ForegroundColor Cyan
claude mcp remove -s user odata1c 2>&1 | Out-Null
claude mcp add -s user odata1c -- uv run --directory $НовыйПуть odata1c mcp
Write-Host ''
Write-Host 'Готово. Проверьте:' -ForegroundColor Green
Write-Host '  claude mcp list          — odata1c должен быть Connected'
Write-Host "  cd $НовыйПуть; claude --resume   — прежние сессии должны быть в списке"
