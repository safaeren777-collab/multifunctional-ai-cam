@echo off
echo Medya dosyaları kopyalanıyor...
mkdir media 2>nul

copy "ilk versiyon açılış videosu bunun görünüşünü lk kısımlara koyabilirsin\WhatsApp Video 2026-05-17 at 17.40.57.mp4" "media\hero_opening.mp4"
copy "pcb ve diğer modüller çıplakken görünümü foto\WhatsApp Image 2026-05-17 at 17.36.39.jpeg" "media\hardware_pcb.jpg"
copy "genel açılma kısmı ve arayüz geçişleri\WhatsApp Video 2026-05-17 at 17.36.36.mp4" "media\ui_transitions.mp4"
copy "guard mod ve fotoları ve videolar\WhatsApp Video 2026-05-17 at 18.01.40.mp4" "media\guard_demo.mp4"
copy "guard mod ve fotoları ve videolar\alert send foto.jpeg" "media\guard_alert.jpg"
copy "guard mod ve fotoları ve videolar\telegrama düşen fotolar.jpeg" "media\guard_telegram.jpg"
copy "ai mod\ai mod dısarıdan nerenin çektiğinin görüntüüsü.jpeg" "media\ai_capture.jpg"
copy "ai mod\çekilen görüntünün ai tarafından yorumlanıp ekrana yansıtılmış versiyonu .jpeg" "media\ai_result_oled.jpg"
copy "ai mod\telegrama düşen fotolar.jpeg" "media\ai_telegram.jpg"
copy "klasik modun cihazda durusu\WhatsApp Image 2026-05-17 at 18.12.56.jpeg" "media\classic_device.jpg"
copy "voice mod\WhatsApp Image 2026-05-17 at 18.34.23.jpeg" "media\voice_oled.jpg"
copy "voice mod\WhatsApp Video 2026-05-17 at 18.34.27.mp4" "media\voice_demo.mp4"
copy "timelapse video ve foto\WhatsApp Image 2026-05-17 at 17.36.36.jpeg" "media\timelapse_frame.jpg"
copy "timelapse video ve foto\WhatsApp Video 2026-05-17 at 17.45.19.mp4" "media\timelapse_demo.mp4"
copy "cad\cad_photo.png" "media\cad_render.png"

echo.
echo Tamamlandı! media\ klasörü hazır.
pause
