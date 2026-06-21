# Hardware Setup Guide — Raspberry Pi Zero W
# Guardian AI Voice Recorder

## Overview

คู่มือนี้อธิบายการต่อสาย I2S microphone กับ Raspberry Pi Zero W  
และการตั้งค่า Device Tree Overlay เพื่อให้ระบบเสียงพร้อมใช้งาน

> **หมายเหตุ:** โค้ด Python (`audio_capture.py`) ไม่ได้ระบุหมายเลข GPIO โดยตรง  
> GPIO ถูกกำหนดผ่าน Device Tree Overlay ใน `/boot/config.txt` เท่านั้น  
> Python เห็น microphone เป็น ALSA device (เช่น `hw:1,0`) ผ่าน sounddevice/PortAudio

---

## Supported Microphone Modules

| Module | Interface | หมายเหตุ |
|--------|-----------|----------|
| INMP441 | I2S | แนะนำ — low noise, 3.3V, wide dynamic range |
| SPH0645LM4H | I2S | Adafruit I2S MEMS Mic, 3.3V |
| ICS-43434 | I2S | High SNR, เหมาะกับงาน voice recognition |

---

## Wiring Diagram

### GPIO Pinout (Pi Zero W → I2S Microphone)

| สัญญาณ | GPIO (BCM) | Physical Pin | สาย microphone |
|--------|-----------|--------------|----------------|
| BCK (Bit Clock) | GPIO 18 | Pin 12 | SCK / BCLK |
| LRCK / WS (Word Select) | GPIO 19 | Pin 35 | WS / LRCLK |
| DIN (Data In จาก mic) | GPIO 20 | Pin 38 | SD / DOUT |
| 3.3V Power | — | Pin 1 หรือ Pin 17 | VDD / 3V3 |
| GND | — | Pin 6, 9, 14, 20, 25, 30, 34, 39 | GND |

> **⚠️ เทียบกับ ESP32 เดิม:**  
> ESP32 ใช้ SCK=GPIO26, WS=GPIO25, DIN=GPIO22  
> ขาเหล่านั้นใช้กับ Pi Zero W **ไม่ได้** — ต้องใช้ BCK=GPIO18, WS=GPIO19, DIN=GPIO20  
> เพราะ Pi Zero W มีเพียง I2S hardware controller เดียว ที่ fixed อยู่บน GPIO เหล่านี้

### INMP441 Wiring (ตัวอย่างที่แนะนำ)

```
Pi Zero W                    INMP441
─────────────────────────────────────────
Pin 1  (3.3V)    ──────────  VDD
Pin 6  (GND)     ──────────  GND
Pin 12 (GPIO18)  ──────────  SCK
Pin 35 (GPIO19)  ──────────  WS
Pin 38 (GPIO20)  ──────────  SD
                             L/R → GND  (เพื่อให้ output บน Left channel)
```

> **L/R pin:** ต่อ L/R กับ GND เพื่อเลือก Left channel  
> ซึ่งตรงกับ `I2S_CHANNEL_FMT_ONLY_LEFT` ที่ ESP32 ใช้

---

## Device Tree Overlay Setup

### วิธีเพิ่มใน /boot/config.txt

เปิดไฟล์:
```bash
sudo nano /boot/config.txt
```

เพิ่มบรรทัดต่อไปนี้ที่ท้ายไฟล์:

```ini
# ─── Guardian AI: I2S Microphone ──────────────────────────────────
# เปิดใช้งาน I2S hardware บน GPIO18 (BCK), GPIO19 (LRCK), GPIO20 (DIN)
dtoverlay=i2s-mmap

# หมายเหตุ: ถ้าใช้ HiFiBerry DAC หรือ Google Voice HAT ให้เปลี่ยนเป็น:
# dtoverlay=hifiberry-dac
# dtoverlay=googlevoicehat-soundcard
# ─────────────────────────────────────────────────────────────────
```

บันทึกไฟล์แล้ว reboot:
```bash
sudo reboot
```

---

## ตรวจสอบหลัง Reboot

### 1. ดู audio device ที่ระบบเห็น
```bash
arecord -l
```
ควรเห็นผลลัพธ์ประมาณนี้:
```
**** List of CAPTURE Hardware Devices ****
card 1: sndrpii2scard [snd_rpi_i2s_card], device 0: simple-card_codec_link ...
  Subdevices: 1/1
  Subdevice #0: subdevice #0
```

### 2. ทดสอบบันทึกเสียง
```bash
arecord -D hw:1,0 -f S16_LE -r 16000 -c 1 -d 5 test.wav
aplay test.wav
```

### 3. ดู ALSA device name สำหรับใส่ใน config
```bash
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

---

## ALSA Device Name ที่ใช้กับ audio_capture.py

| กรณี | Device Name | หมายเหตุ |
|------|-------------|----------|
| ค่า default | `"default"` | ถ้า ALSA default ชี้มาที่ I2S mic ถูกต้อง |
| ระบุตรงๆ | `"hw:1,0"` | Card 1, Device 0 (ตาม arecord -l) |
| ผ่าน ALSA alias | `"plughw:1,0"` | ให้ ALSA แปลง sample rate อัตโนมัติ |

กำหนดผ่าน config:
```python
from audio_capture import AudioCapture

capture = AudioCapture(
    publish_fn=my_mqtt_publish,
    device="hw:1,0",          # ระบุ device ตรงๆ ถ้า "default" ไม่ถูกต้อง
)
```

---

## Troubleshooting

| อาการ | สาเหตุที่เป็นไปได้ | วิธีแก้ |
|-------|------------------|--------|
| `AudioDeviceError: ไม่พบ ALSA device "default"` | dtoverlay ยังไม่ได้เพิ่ม หรือ reboot ยังไม่ได้ | ตรวจสอบ /boot/config.txt แล้ว reboot |
| `arecord -l` ไม่เห็น I2S card | สายไฟผิด หรือ mic เสีย | ตรวจสอบ wiring ตามตารางด้านบน |
| เสียงเงียบ / level ต่ำมาก | L/R pin ไม่ได้ต่อ GND | ต่อ L/R ของ INMP441 กับ GND |
| เสียงผิดเพี้ยน / clipping | ระดับสัญญาณสูงเกิน | ห่าง microphone ออกมา หรือปรับ gain |
| `ALSA lib pcm.c: ... unable to open slave` | device name ผิด | รัน `arecord -l` แล้วใช้ชื่อที่ถูกต้อง |

---

## อ้างอิง ESP32 เดิม

| ESP32 (main.c) | Pi Zero W |
|----------------|-----------|
| `I2S_SCK_PIN 26` | GPIO 18 (Pin 12) |
| `I2S_WS_PIN 25` | GPIO 19 (Pin 35) |
| `I2S_DIN_PIN 22` | GPIO 20 (Pin 38) |
| `I2S_SAMPLE_RATE 16000` | 16000 Hz |
| `I2S_BITS_PER_SAMPLE_32BIT` → shift >> 16 | int16 โดยตรงจาก ALSA |
| `I2S_CHANNEL_FMT_ONLY_LEFT` | channels=1 (mono) |
| `AUDIO_CHUNK_SAMPLES 2048` | blocksize=2048 |
