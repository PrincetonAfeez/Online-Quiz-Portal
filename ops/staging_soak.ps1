# QuizOps staging soak + ops drills (Windows / Docker Compose).
# Run from the repo root:  powershell -File ops/staging_soak.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$DrillDir = Join-Path $Root "ops\drills"
New-Item -ItemType Directory -Force -Path $DrillDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Report = Join-Path $DrillDir "soak-$Stamp.json"
$BackupFile = Join-Path $DrillDir "quizops-$Stamp.dump"
$CookieJar = Join-Path $env:TEMP "quizops-soak-cookies.txt"

function Step($name, $script) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $script
    Write-Host "OK: $name" -ForegroundColor Green
}

$results = [ordered]@{
    executed_at = (Get-Date).ToUniversalTime().ToString("o")
    environment = "staging (docker compose / Postgres)"
    steps       = [ordered]@{}
}

Step "compose up" {
    docker compose up -d --build
    # Wait for web health (probe inside the container — host path quirks vary).
    $deadline = (Get-Date).AddMinutes(10)
    do {
        Start-Sleep -Seconds 5
        docker compose exec -T web curl -fsS http://127.0.0.1:8000/healthz/ 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        if ((Get-Date) -gt $deadline) {
            docker compose logs --tail=40 web
            throw "web healthz timeout"
        }
        Write-Host "waiting for web..."
    } while ($true)
}

Step "migrate" {
    docker compose exec -T web python manage.py migrate --noinput
}

Step "deploy check" {
    docker compose exec -T web python manage.py check --deploy
}

Step "readyz" {
    $body = docker compose exec -T web curl -fsS http://127.0.0.1:8000/readyz/
    Write-Host $body
    if ($body -notmatch '"database"\s*:\s*"ok"') { throw "readyz database not ok: $body" }
    if ($body -notmatch '"cache"\s*:\s*"ok"') { throw "readyz cache not ok: $body" }
    $results.steps.readyz = "$body".Trim()
}

Step "seed_demo" {
    docker compose exec -T web python manage.py seed_demo
}

Step "backup pg_dump" {
    docker compose exec -T postgres pg_dump -U quizops -d quizops --format=custom -f /tmp/quizops.dump
    docker compose cp postgres:/tmp/quizops.dump $BackupFile
    if (-not (Test-Path $BackupFile)) { throw "backup missing at $BackupFile" }
    $results.steps.backup_bytes = (Get-Item $BackupFile).Length
}

Step "restore into scratch DB" {
    docker compose exec -T postgres dropdb -U quizops --if-exists quizops_restore_test
    docker compose exec -T postgres createdb -U quizops quizops_restore_test
    docker compose cp $BackupFile postgres:/tmp/restore.dump
    docker compose exec -T postgres pg_restore -U quizops -d quizops_restore_test --clean --if-exists /tmp/restore.dump
    $counts = docker compose exec -T postgres psql -U quizops -d quizops_restore_test -t -A -c "select (select count(*) from assessments_attempt)||','||(select count(*) from assessments_attemptfinalization)||','||(select count(*) from accounts_user);"
    Write-Host "restore counts attempts,finalizations,users = $counts"
    $results.steps.restore_counts = "$counts".Trim()
    docker compose exec -T postgres dropdb -U quizops quizops_restore_test
}

Step "celery beat / sweep under load" {
    1..5 | ForEach-Object {
        docker compose exec -T web python manage.py shell -c "from assessments.tasks import finalize_expired_attempts as f; print('sweep', f())"
    }
    docker compose exec -T worker celery -A config inspect ping
    $results.steps.celery_ping = "ok"
}

Step "password reset SMTP (Mailpit)" {
    docker compose exec -T web python ops/drill_password_reset.py
    Start-Sleep -Seconds 2
    $messages = curl.exe -fsS "http://127.0.0.1:8025/api/v1/messages"
    $parsed = $messages | ConvertFrom-Json
    if (-not $parsed.total -or [int]$parsed.total -lt 1) {
        throw "Mailpit received no messages after password reset. Body: $messages"
    }
    $results.steps.mailpit_total = [int]$parsed.total
    Write-Host "Mailpit messages: $($parsed.total)  UI: http://127.0.0.1:8025"
}

Step "MFA enforcement smoke" {
    $out = docker compose exec -T web python ops/drill_mfa_smoke.py
    Write-Host $out
    if ("$out" -notmatch "mfa_ok") { throw "MFA smoke failed" }
    $results.steps.mfa_redirect = "ok"
}

$results.result = "pass"
$results | ConvertTo-Json -Depth 6 | Set-Content -Path $Report -Encoding utf8
Copy-Item $Report (Join-Path $DrillDir "last-soak.json") -Force
@"
{
  "executed_at": "$($results.executed_at)",
  "environment": "staging (docker compose / Postgres)",
  "backup_file": "$(Split-Path -Leaf $BackupFile)",
  "restore_counts": "$($results.steps.restore_counts)",
  "mailpit_total": $($results.steps.mailpit_total),
  "mfa_redirect": "$($results.steps.mfa_redirect)",
  "result": "pass"
}
"@ | Set-Content (Join-Path $DrillDir "last-drill.json") -Encoding utf8

Write-Host "`nSoak PASS. Report: $Report" -ForegroundColor Green
Write-Host "Backup: $BackupFile"
Write-Host "Mailpit: http://127.0.0.1:8025"
Write-Host "App:     http://127.0.0.1:8000"
