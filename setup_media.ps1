$base = $PSScriptRoot
$media = "$base\media"
New-Item -ItemType Directory -Force -Path $media | Out-Null

# Dosyaları timestamp'e göre bul ve kopyala
$map = @{
    "17.40.57" = "hero_opening.mp4"
    "17.36.39" = "hardware_pcb.jpg"
    "17.36.36.mp4" = "ui_transitions.mp4"
    "18.01.40" = "guard_demo.mp4"
    "alert send foto" = "guard_alert.jpg"
    "18.34.23" = "voice_oled.jpg"
    "18.34.27" = "voice_demo.mp4"
    "18.12.56" = "classic_device.jpg"
}

# Guard klasöründeki "telegrama düşen" dosyalar
$guardFolder = Get-ChildItem $base -Directory | Where-Object { $_.Name -like "guard*" }
$aiFolder    = Get-ChildItem $base -Directory | Where-Object { $_.Name -like "ai*" }

# Timestamp tabanlı kopyalama
Get-ChildItem $base -Recurse -File | ForEach-Object {
    foreach ($key in $map.Keys) {
        if ($_.Name -like "*$key*") {
            $dst = "$media\$($map[$key])"
            if (-not (Test-Path $dst)) {
                Copy-Item $_.FullName -Destination $dst -Force
                Write-Host "OK: $($map[$key])  ← $($_.Name)" -ForegroundColor Green
            }
        }
    }
}

# "telegrama düşen fotolar" — guard klasöründen
if ($guardFolder) {
    $tg = Get-ChildItem $guardFolder.FullName | Where-Object { $_.Name -like "*telegrama*" }
    if ($tg) {
        Copy-Item $tg.FullName "$media\guard_telegram.jpg" -Force
        Write-Host "OK: guard_telegram.jpg" -ForegroundColor Green
    }
}

# AI mod dosyaları
if ($aiFolder) {
    $aiFiles = Get-ChildItem $aiFolder.FullName
    $aiFiles | ForEach-Object {
        if ($_.Name -like "*nerenin*" -or $_.Name -like "*d?saridan*" -or $_.Name -like "*capture*") {
            Copy-Item $_.FullName "$media\ai_capture.jpg" -Force
            Write-Host "OK: ai_capture.jpg" -ForegroundColor Green
        }
        elseif ($_.Name -like "*yorumlan*" -or $_.Name -like "*ekrana*" -or $_.Name -like "*versiyonu*") {
            Copy-Item $_.FullName "$media\ai_result_oled.jpg" -Force
            Write-Host "OK: ai_result_oled.jpg" -ForegroundColor Green
        }
        elseif ($_.Name -like "*telegrama*") {
            Copy-Item $_.FullName "$media\ai_telegram.jpg" -Force
            Write-Host "OK: ai_telegram.jpg" -ForegroundColor Green
        }
    }
}

# Hero klasöründeki video
$heroFolder = Get-ChildItem $base -Directory | Where-Object { $_.Name -like "*versiyon*" -or $_.Name -like "*hero*" }
if ($heroFolder -and -not (Test-Path "$media\hero_opening.mp4")) {
    $hv = Get-ChildItem $heroFolder.FullName -Filter "*.mp4" | Select-Object -First 1
    if ($hv) {
        Copy-Item $hv.FullName "$media\hero_opening.mp4" -Force
        Write-Host "OK: hero_opening.mp4" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "=== media/ klasoru ===" -ForegroundColor Cyan
Get-ChildItem $media | ForEach-Object { Write-Host "  $($_.Name)" }
Read-Host "Enter'a bas"
