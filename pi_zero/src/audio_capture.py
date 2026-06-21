"""
audio_capture.py
================
Python firmware สำหรับ Raspberry Pi Zero W
ทำหน้าที่บันทึกเสียงผ่าน ALSA (sounddevice) และส่ง raw PCM ผ่าน callback

เทียบเท่ากับ:
  - init_i2s_audio()      → AudioCapture.__init__() + start()
  - audio_record_task()   → _audio_callback() (sounddevice callback thread)
  - calculate_dbfs()      → _calculate_dbfs()

พารามิเตอร์เสียงต้องตรงกับ esp32-reference/esp32/main/main.c:
  SAMPLE_RATE    = 16000 Hz
  CHANNELS       = 1 (mono)
  SAMPLE_WIDTH   = 2 bytes (int16, เทียบเท่า 32-bit I2S shift >> 16 บนฝั่ง ESP32)
  CHUNK_SAMPLES  = 2048 samples per publish

Hardware note:
  GPIO wiring สำหรับ I2S microphone (INMP441/SPH0645) อยู่ใน:
  pi_zero/README_HARDWARE.md
  GPIO ไม่ได้ระบุในโค้ด Python — ถูกกำหนดผ่าน Device Tree Overlay ใน /boot/config.txt
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default audio constants — ตรงกับ main.c ทุกค่า
# ---------------------------------------------------------------------------
DEFAULT_SAMPLE_RATE: int = 16_000          # I2S_SAMPLE_RATE
DEFAULT_CHANNELS: int = 1                  # I2S_CHANNEL_FMT_ONLY_LEFT → mono
DEFAULT_SAMPLE_WIDTH: int = 2              # int16 = 2 bytes (ESP32 shift raw_buf[i] >> 16)
DEFAULT_CHUNK_SAMPLES: int = 2_048         # AUDIO_CHUNK_SAMPLES
DEFAULT_GATE_THRESHOLD_DBFS: float = -45.0 # AUDIO_GATE_THRESHOLD_DBFS
DEFAULT_GATE_HOLD_CHUNKS: int = 8          # AUDIO_GATE_HOLD_CHUNKS
DEFAULT_ALSA_DEVICE: str = "default"       # ALSA device name — ปรับผ่าน config ได้


# ---------------------------------------------------------------------------
# Custom Exception
# ---------------------------------------------------------------------------
class AudioDeviceError(RuntimeError):
    """
    ยกขึ้นเมื่อ ALSA device ไม่พบ หรือไม่รองรับ audio input

    เทียบเท่ากับ error path ของ i2s_driver_install() / i2s_set_pin() ใน ESP32
    ซึ่งจะคืน ESP_ERR และหยุด task ทันที
    """


# ---------------------------------------------------------------------------
# AudioCapture
# ---------------------------------------------------------------------------
class AudioCapture:
    """
    บันทึกเสียงจาก ALSA device และส่ง raw PCM bytes ผ่าน publish_fn

    Usage
    -----
    >>> def my_publish(pcm: bytes) -> None:
    ...     mqtt_client.publish(topic, pcm, qos=0)
    ...
    >>> capture = AudioCapture(publish_fn=my_publish)
    >>> capture.start()
    >>> # ... รอจนกว่าจะต้องหยุด ...
    >>> capture.stop()
    """

    def __init__(
        self,
        publish_fn: Callable[[bytes], None],
        device: str = DEFAULT_ALSA_DEVICE,
        config: Optional[dict] = None,
    ) -> None:
        """
        Parameters
        ----------
        publish_fn:
            Callable รับ raw PCM bytes (int16 little-endian) และส่งออกไป
            (จะถูกเรียกจาก audio thread — ต้อง thread-safe)
        device:
            ชื่อ ALSA device สำหรับ sounddevice (default="default")
            ห้าม auto-detect — ถ้า device ไม่พบจะ raise AudioDeviceError
        config:
            dict สำหรับ override ค่า default ได้ เช่น:
            {"sample_rate": 16000, "chunk_samples": 2048,
             "gate_threshold_dbfs": -45.0, "gate_hold_chunks": 8}
        """
        cfg = config or {}

        self._publish_fn = publish_fn
        self._device = device

        # Audio parameters — ตรงกับ main.c
        self._sample_rate: int = int(cfg.get("sample_rate", DEFAULT_SAMPLE_RATE))
        self._channels: int = int(cfg.get("channels", DEFAULT_CHANNELS))
        self._chunk_samples: int = int(cfg.get("chunk_samples", DEFAULT_CHUNK_SAMPLES))

        # Noise gate parameters — ตรงกับ main.c
        self._gate_threshold: float = float(
            cfg.get("gate_threshold_dbfs", DEFAULT_GATE_THRESHOLD_DBFS)
        )
        self._gate_hold_chunks: int = int(
            cfg.get("gate_hold_chunks", DEFAULT_GATE_HOLD_CHUNKS)
        )

        # Noise gate state (เทียบเท่า gate_hold_chunks, gate_open ใน audio_record_task)
        self._hold_remaining: int = 0
        self._gate_open: bool = False

        # sounddevice stream (สร้างใน start())
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()       # ป้องกัน race condition บน gate state
        self._running = False

        logger.info(
            "AudioCapture init: device=%r sample_rate=%d channels=%d "
            "chunk_samples=%d gate=%.1f dBFS hold=%d chunks",
            self._device, self._sample_rate, self._channels,
            self._chunk_samples, self._gate_threshold, self._gate_hold_chunks,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _calculate_dbfs(self, samples: np.ndarray) -> float:
        """
        คำนวณ RMS level ของ PCM samples แล้วแปลงเป็น dBFS

        เทียบเท่า calculate_dbfs() ใน main.c (L66-83)

        Parameters
        ----------
        samples:
            numpy array dtype int16, shape (N,) หรือ (N, 1)

        Returns
        -------
        float: dBFS value, หรือ -96.0 ถ้าสัญญาณเงียบ/ว่าง
        """
        samples = samples.flatten()
        if samples.size == 0:
            return -96.0

        # Normalize int16 → float [-1.0, 1.0]  (เทียบเท่า / 32768.0f ใน C)
        normalized = samples.astype(np.float64) / 32768.0
        rms = math.sqrt(float(np.mean(normalized ** 2)))

        if rms < 1e-6:
            return -96.0

        return 20.0 * math.log10(rms)

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        """
        sounddevice InputStream callback — ถูกเรียกจาก audio thread ทุก chunk

        เทียบเท่า loop body ของ audio_record_task() ใน main.c (L392-448)

        Noise gate logic (ตรงกับ ESP32 ทุกกรณี):
          - dBFS >= threshold       → should_publish=True, reset hold=HOLD_CHUNKS
          - dBFS <  threshold + hold > 0  → should_publish=True, hold-- (tail padding)
          - dBFS <  threshold + hold == 0 → should_publish=False (ไม่ส่งเลย)

        Parameters
        ----------
        indata: numpy array shape (frames, channels) dtype int16
        frames: จำนวน sample ที่ได้รับ (ควรเท่ากับ blocksize=chunk_samples)
        time_info: timing info จาก sounddevice (ไม่ใช้)
        status: sounddevice CallbackFlags — ใช้ตรวจสอบ underflow/overflow
        """
        # --- 1. ตรวจสอบ ALSA error (เทียบเท่า ret != ESP_OK ใน C) ---
        if status:
            # input_overflow: เสียงขาดหาย (buffer ไม่ทัน)
            # input_underflow: ได้รับน้อยกว่าที่คาด
            logger.warning("Audio callback status: %s — chunk skipped", status)
            return

        # --- 2. แปลง indata → int16 bytes (เทียบเท่า chunk_buf + >> 16 ใน C) ---
        # sounddevice ส่งมาเป็น dtype='int16' แล้ว ไม่ต้อง shift
        # (ESP32 อ่าน 32-bit แล้ว shift >> 16 เพื่อได้ int16 — ที่นี่ได้ int16 ตรงๆ)
        pcm_int16: np.ndarray = indata[:, 0] if indata.ndim == 2 else indata

        # --- 3. คำนวณ dBFS (เทียบเท่า calculate_dbfs() ใน C) ---
        chunk_dbfs = self._calculate_dbfs(pcm_int16)

        # --- 4. Noise gate state machine ---
        should_publish = False

        with self._lock:
            if chunk_dbfs >= self._gate_threshold:
                # เสียงดังพอ → เปิด gate + reset hold counter
                self._hold_remaining = self._gate_hold_chunks
                should_publish = True

                if not self._gate_open:
                    self._gate_open = True
                    logger.info("🎙️  Audio gate opened at %.1f dBFS", chunk_dbfs)

            elif self._hold_remaining > 0:
                # เสียงเงียบแต่ยัง hold อยู่ → ส่งต่อ (tail padding)
                self._hold_remaining -= 1
                should_publish = True

            else:
                # gate ปิดสนิท → ไม่ส่งเลย
                if self._gate_open:
                    self._gate_open = False
                    logger.info("🔇  Audio gate closed at %.1f dBFS", chunk_dbfs)
                # should_publish ยังเป็น False → ไม่ทำอะไร

        # --- 5. ส่ง raw PCM bytes เฉพาะเมื่อ gate เปิด/hold ---
        if should_publish:
            pcm_bytes: bytes = pcm_int16.astype("<i2").tobytes()  # little-endian int16
            try:
                self._publish_fn(pcm_bytes)
            except Exception:
                logger.exception("publish_fn raised exception — chunk dropped")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @staticmethod
    def list_devices() -> list[dict]:
        """
        คืนรายชื่อ audio input devices ที่พร้อมใช้งานบนระบบ

        เทียบเท่า error log ของ ESP32 ที่แสดง available I2S ports
        ใช้สำหรับ debug และแสดงใน AudioDeviceError message

        Returns
        -------
        list of dict: แต่ละ dict มี 'index', 'name', 'max_input_channels', 'default_samplerate'
        """
        devices = []
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                devices.append({
                    "index": i,
                    "name": dev["name"],
                    "max_input_channels": dev["max_input_channels"],
                    "default_samplerate": dev["default_samplerate"],
                })
        return devices

    def start(self) -> None:
        """
        เริ่มบันทึกเสียงจาก ALSA device

        เทียบเท่า init_i2s_audio() + xTaskCreate(audio_record_task) ใน main.c

        Error Handling
        --------------
        - ถ้า device ไม่พบหรือไม่มี input channel → raise AudioDeviceError
          พร้อม list available input devices ใน message
        - ถ้า sounddevice ไม่สามารถเปิด stream ได้ → ยก exception ให้ caller จัดการ
          (เทียบเท่า ESP_ERROR_CHECK ที่หยุดระบบบน ESP32)

        Raises
        ------
        AudioDeviceError: device ไม่พบหรือใช้งาน input ไม่ได้
        sd.PortAudioError: ALSA error ระดับ driver
        """
        if self._running:
            logger.warning("AudioCapture.start() called while already running — ignored")
            return

        # --- ตรวจสอบ device ก่อนเปิด stream ---
        self._validate_device()

        logger.info(
            "Starting audio capture: device=%r %d Hz mono int16 blocksize=%d",
            self._device, self._sample_rate, self._chunk_samples,
        )

        try:
            self._stream = sd.InputStream(
                device=self._device,
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype="int16",                  # รับ int16 ตรงๆ (ไม่ต้อง shift เหมือน ESP32)
                blocksize=self._chunk_samples,  # AUDIO_CHUNK_SAMPLES = 2048
                callback=self._audio_callback,
                latency="low",
            )
            self._stream.start()
            self._running = True
            logger.info("✅ Audio capture started successfully")

        except sd.PortAudioError as exc:
            self._stream = None
            logger.error("Failed to open audio stream on device %r: %s", self._device, exc)
            raise

    def stop(self) -> None:
        """
        หยุดบันทึกเสียงอย่าง graceful

        เทียบเท่า vTaskDelete(audio_record_task) บน ESP32
        รับประกันว่า callback จะไม่ถูกเรียกหลัง stop() คืนค่า
        """
        if not self._running:
            logger.warning("AudioCapture.stop() called while not running — ignored")
            return

        logger.info("Stopping audio capture...")

        if self._stream is not None:
            try:
                self._stream.stop()   # รอให้ callback ปัจจุบันเสร็จก่อน
                self._stream.close()
            except Exception:
                logger.exception("Error while stopping audio stream")
            finally:
                self._stream = None

        self._running = False

        # Reset gate state
        with self._lock:
            self._hold_remaining = 0
            self._gate_open = False

        logger.info("🛑 Audio capture stopped")

    @property
    def is_running(self) -> bool:
        """True ถ้า stream กำลังทำงานอยู่"""
        return self._running

    # ------------------------------------------------------------------
    # Private validation
    # ------------------------------------------------------------------

    def _validate_device(self) -> None:
        """
        ตรวจสอบว่า self._device มีอยู่จริงและรับ input ได้

        ถ้าไม่ผ่าน → raise AudioDeviceError พร้อม list available input devices
        (ตามที่กำหนดใน requirements: ห้าม auto-detect, ถ้าไม่เจอให้ raise ทันที)
        """
        try:
            device_info = sd.query_devices(self._device, kind="input")
        except (ValueError, sd.PortAudioError) as exc:
            available = self.list_devices()
            available_str = "\n".join(
                f"  [{d['index']}] {d['name']} "
                f"(inputs={d['max_input_channels']}, "
                f"rate={int(d['default_samplerate'])} Hz)"
                for d in available
            ) or "  (ไม่พบ audio input device ใดๆ ในระบบ)"

            raise AudioDeviceError(
                f"ไม่พบ ALSA device {self._device!r} หรือไม่รองรับ audio input\n"
                f"ข้อผิดพลาด: {exc}\n\n"
                f"Audio input devices ที่พบในระบบ:\n{available_str}\n\n"
                f"วิธีแก้:\n"
                f"  1. ตรวจสอบการต่อสาย I2S microphone (ดู pi_zero/README_HARDWARE.md)\n"
                f"  2. ตรวจสอบ /boot/config.txt ว่าเพิ่ม dtoverlay แล้ว\n"
                f"  3. รัน: arecord -l  เพื่อดู device ที่ระบบเห็น\n"
                f"  4. ระบุชื่อ device ที่ถูกต้องผ่าน config={{'device': 'hw:1,0'}}"
            ) from exc

        # ตรวจสอบเพิ่มว่ามี input channel จริงๆ
        if device_info["max_input_channels"] < self._channels:
            available = self.list_devices()
            available_str = "\n".join(
                f"  [{d['index']}] {d['name']} (inputs={d['max_input_channels']})"
                for d in available
            )
            raise AudioDeviceError(
                f"Device {self._device!r} มี max_input_channels="
                f"{device_info['max_input_channels']} "
                f"แต่ต้องการ {self._channels} channel\n\n"
                f"Audio input devices ที่พบในระบบ:\n{available_str}"
            )

        logger.info(
            "Device validated: %r (%d input channels, default %.0f Hz)",
            device_info["name"],
            device_info["max_input_channels"],
            device_info["default_samplerate"],
        )
