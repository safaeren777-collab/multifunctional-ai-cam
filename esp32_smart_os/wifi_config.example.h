#pragma once

// ────────────────────────────────────────────────────────────
//  Local configuration template
//
//  1) Copy this file to wifi_config.h (same folder).
//  2) Fill in your own values below.
//  3) wifi_config.h is gitignored — your real credentials never
//     leave your machine.
// ────────────────────────────────────────────────────────────

// WiFi network the device joins on boot
#define WIFI_SSID     "YOUR_WIFI_SSID"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

// VPS backend address (the machine running vps_backend/main.py)
#define VPS_HOST "your.vps.ip.or.hostname"
#define VPS_PORT 8003
