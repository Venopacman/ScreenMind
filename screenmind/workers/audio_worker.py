"""
Audio Worker — Call Tracking and Meeting Transcription
Detects calls from every visible window (and, on macOS, which apps hold the
mic), records each call's start and end in the meetings table, and, when
meeting transcription is on, captures dual-channel audio (system + mic),
transcribes it with Gemma 4's native audio encoder and writes a summary.
"""

import io
import logging
import time
import threading
import wave
from datetime import datetime
from typing import Optional, List

import numpy as np

from screenmind.config import settings
from screenmind.storage.database import Database
from screenmind.workers.call_detection import CallMatch, match_call, owner_on_mic

logger = logging.getLogger("screenmind.workers.audio_worker")


# Call detection runs on its own thread at this interval. It must not share
# the capture loop: a slow screen grab there (30 s+) starves the check.
CHECK_INTERVAL_S = 5
# A call starts after this many detections in a row (CHECK_INTERVAL_S apart). Filters
# out a window flashing past and short mic use like Slack voice clips.
CONFIRM_CHECKS = 2
# A call ends when neither its window nor its mic use was seen for this
# long. Covers switching browser tabs away from Meet while muted. The end
# time saved is the last time the call was seen, not when the grace ran out.
END_GRACE_S = 120
# While a call runs, save its duration this often so a crash keeps it.
DURATION_SAVE_EVERY_S = 60
# How often to retry reading the room URL when the first try failed.
URL_RETRY_EVERY_S = 30


class AudioWorker:
    """
    Background worker for call tracking and meeting transcription.
    - Detects calls from all visible windows, matching owner app and title,
      plus mic use where the OS reports it (see call_detection.py)
    - Records start/end, app, window title and room URL for every call
    - With meeting transcription on: captures system audio (loopback) +
      microphone, transcribes chunks with Gemma 4 (via llama-server) and
      generates a structured summary when the call ends
    """

    def __init__(self, database: Database):
        self._db = database
        self._running = False
        self._available = False
        self._lock = threading.RLock()
        self._detect_thread: Optional[threading.Thread] = None
        self._detect_stop = threading.Event()

        # Call state
        self._in_meeting = False
        self._meeting_id: Optional[int] = None
        self._meeting_app: Optional[str] = None
        self._meeting_owner: Optional[str] = None  # window owner / mic app, for keep-alive
        self._meeting_title: Optional[str] = None
        self._meeting_url: Optional[str] = None
        self._session_start: Optional[datetime] = None
        self._last_seen: float = 0
        self._last_duration_save: float = 0
        self._last_url_try: float = 0

        # Candidate call, waiting for CONFIRM_CHECKS detections
        self._pending: Optional[CallMatch] = None
        self._pending_count = 0
        self._pending_since: float = 0

        # Transcription state
        self._recording = False
        self._session_transcript: List[str] = []
        self._recording_thread: Optional[threading.Thread] = None
        self._stop_recording = threading.Event()

        # Audio config
        self._sample_rate = 16000  # Gemma audio encoder expects 16kHz
        self._chunk_duration = 15  # seconds per transcription chunk

        # Check transcription backend availability
        self._init_transcription()

    def _init_transcription(self):
        """Check that Gemma audio transcription is available via llama-server."""
        try:
            from screenmind.engine import llm_client
            if llm_client.is_available():
                self._available = True
                logger.info("Gemma audio transcription ready (via llama-server).")
            else:
                logger.warning("llama-server not available — meeting transcription disabled")
                self._available = False
        except Exception as e:
            logger.warning(f"Transcription init failed: {e}")
            self._available = False

    @property
    def is_available(self) -> bool:
        """Whether calls will be transcribed (tracking works regardless)."""
        return self._available and settings.meeting_transcription

    @property
    def in_meeting(self) -> bool:
        return self._in_meeting

    # ── Call detection ───────────────────────────────────────────────

    def start(self):
        """Start the call detection thread. It runs while capture is paused too."""
        if self._detect_thread and self._detect_thread.is_alive():
            return
        self._detect_stop.clear()
        self._detect_thread = threading.Thread(
            target=self._detect_loop, name="call-detection", daemon=True)
        self._detect_thread.start()

    def stop(self):
        """Stop detection and end the current call (shutdown)."""
        self._detect_stop.set()
        self.force_stop()

    def _detect_loop(self):
        while not self._detect_stop.wait(CHECK_INTERVAL_S):
            try:
                self.check_calls()
            except Exception as e:
                logger.debug(f"Call detection failed: {e}")

    def check_calls(self):
        """One detection pass: read every visible window and the apps using
        the mic, then advance the call state machine."""
        from screenmind.capture.window import list_visible_windows, get_mic_apps
        if not settings.meeting_apps_list:
            return
        self.update(list_visible_windows(), get_mic_apps())

    def update(self, windows: list, mic_apps: Optional[set], now: Optional[float] = None):
        """Advance the call state machine with one snapshot of the screen."""
        now = time.time() if now is None else now
        match = match_call(windows, mic_apps, settings.meeting_apps_list)
        with self._lock:
            if self._in_meeting:
                if match and match.app == self._meeting_app:
                    self._seen(match, now)
                    return
                if owner_on_mic(self._meeting_owner, mic_apps):
                    self._seen(None, now)
                    return
                if match:
                    # A different call took over: close this one, confirm the new one
                    logger.info(f"Call switched from {self._meeting_app} to {match.app}")
                    self._stop_meeting(end=self._last_seen)
                elif now - self._last_seen > END_GRACE_S:
                    self._stop_meeting(end=self._last_seen)
                    return
                else:
                    return

            if not match:
                self._pending = None
                self._pending_count = 0
                return
            if self._pending and self._pending.app == match.app:
                self._pending_count += 1
            else:
                self._pending = match
                self._pending_count = 1
                self._pending_since = now
            if self._pending_count >= CONFIRM_CHECKS:
                since = self._pending_since
                self._pending = None
                self._pending_count = 0
                self._start_meeting(match, start=since, now=now)

    def _seen(self, match: Optional[CallMatch], now: float):
        """The current call is still running."""
        self._last_seen = now
        title = url = duration = None
        if match and match.title and not self._meeting_title:
            title = self._meeting_title = match.title
        if match and match.is_browser and not self._meeting_url \
                and now - self._last_url_try >= URL_RETRY_EVERY_S:
            url = self._meeting_url = self._read_url(match, now)
        if now - self._last_duration_save >= DURATION_SAVE_EVERY_S:
            self._last_duration_save = now
            duration = self._minutes(now)
        if self._meeting_id and (title or url or duration is not None):
            try:
                self._db.update_meeting_call_info(
                    self._meeting_id, window_title=title, url=url, duration_minutes=duration)
            except Exception as e:
                logger.warning(f"Could not update meeting {self._meeting_id}: {e}")

    def _read_url(self, match: CallMatch, now: float) -> Optional[str]:
        """Room URL of a browser call window, sanitized like other stored URLs."""
        self._last_url_try = now
        from screenmind.capture.window import get_window_url
        from screenmind.privacy.url_filter import sanitize_url
        return sanitize_url(get_window_url(match.pid, match.bounds))

    def _minutes(self, now: float) -> float:
        if not self._session_start:
            return 0
        return round(max(0.0, now - self._session_start.timestamp()) / 60, 1)

    def _start_meeting(self, match: CallMatch, start: float, now: float):
        """Begin tracking a call, and recording it if transcription is on."""
        self._in_meeting = True
        self._meeting_app = match.app
        self._meeting_owner = match.owner
        self._meeting_title = match.title
        self._meeting_url = self._read_url(match, now) if match.is_browser else None
        self._session_start = datetime.fromtimestamp(start)
        self._session_transcript = []
        self._last_seen = now
        self._last_duration_save = now

        self._recording = self._can_record()
        self._meeting_id = self._db.insert_meeting(
            start_time=self._session_start,
            app_name=match.app,
            transcript="" if self._recording else None,
            summary="" if self._recording else None,
            window_title=match.title,
            url=self._meeting_url,
        )
        logger.info(f"Call started ({match.app}"
                    f"{', ' + match.title if match.title else ''})"
                    f"{' — recording...' if self._recording else ''}")
        if not self._recording:
            return

        # System-wide overlay notification
        try:
            from screenmind.ui.overlay import show_overlay_notification
            show_overlay_notification(
                title="ScreenMind is Transcribing",
                message=f"Meeting detected in {match.app} — recording audio...",
                duration=4.0,
                color="#ec4899",
            )
        except Exception:
            pass  # Notification is best-effort

        # Start recording in background thread
        self._stop_recording.clear()
        self._recording_thread = threading.Thread(
            target=self._recording_loop, daemon=True
        )
        self._recording_thread.start()

    def _can_record(self) -> bool:
        """Transcription is on and the active model can take audio."""
        if not self.is_available:
            return False
        try:
            from screenmind.engine import model_manager
            if model_manager.is_audio_capable():
                return True
            logger.info("Call detected but the active model has no audio encoder — tracking only")
        except Exception as e:
            logger.debug(f"Audio capability check failed: {e}")
        return False

    def _stop_meeting(self, end: Optional[float] = None):
        """End the call. Saves the end time and, if it was recorded, the
        transcript, then triggers summary generation."""
        with self._lock:
            if not self._in_meeting:
                return
            self._in_meeting = False
            meeting_id = self._meeting_id
            recording = self._recording
            self._recording = False
            end_time = datetime.fromtimestamp(end) if end else datetime.now()
            duration = round(max(0.0, (end_time - self._session_start).total_seconds()) / 60, 1) \
                if self._session_start else 0
            app = self._meeting_app

            self._meeting_id = None
            self._meeting_app = None
            self._meeting_owner = None
            self._meeting_title = None
            self._meeting_url = None
            self._session_start = None

        if not recording:
            logger.info(f"Call ended ({app}, {duration:.1f} min)")
            if meeting_id:
                self._db.end_meeting(meeting_id, end_time, duration)
            return

        self._stop_recording.set()
        # Wait for recording thread to finish (skip if called from within it)
        if self._recording_thread and self._recording_thread.is_alive():
            if threading.current_thread() != self._recording_thread:
                self._recording_thread.join(timeout=5)

        full_transcript = "\n".join(self._session_transcript)
        logger.info(f"Meeting ended ({duration:.1f} min, {len(self._session_transcript)} chunks)")

        # System-wide overlay notification
        try:
            from screenmind.ui.overlay import show_overlay_notification
            chunks = len(self._session_transcript)
            show_overlay_notification(
                title="✅ Meeting Recording Complete",
                message=f"{duration:.0f} min recorded • {chunks} audio chunks • Generating summary...",
                duration=5.0,
                color="#10b981",
            )
        except Exception:
            pass

        if meeting_id and full_transcript.strip():
            # Update with transcript (summary comes async)
            self._db.update_meeting(
                meeting_id=meeting_id,
                end_time=end_time,
                duration_minutes=duration,
                transcript=full_transcript,
                summary="⏳ Generating summary...",
            )
            logger.info(f"Transcript saved ({len(full_transcript)} chars, {len(self._session_transcript)} chunks)")
            # Trigger summary in background thread
            summary_thread = threading.Thread(
                target=self._generate_summary,
                args=(meeting_id, full_transcript),
                daemon=True,
            )
            summary_thread.start()
        elif meeting_id:
            logger.warning(f"No transcript to save (session_transcript={len(self._session_transcript)} items)")
            self._db.update_meeting(
                meeting_id=meeting_id,
                end_time=end_time,
                duration_minutes=duration,
                transcript="(No speech detected)",
                summary="No content to summarize.",
            )
        self._session_transcript = []

    def _recording_loop(self):
        """
        Background thread: capture audio in chunks and transcribe.
        Records mic + system audio in parallel, transcribes each
        separately via Gemma for speaker-labeled output.
        """
        try:
            import sounddevice as sd
        except ImportError:
            logger.warning("sounddevice not installed — cannot record")
            return

        # Find system loopback device once (not every chunk)
        loopback_id = self._find_loopback_device(sd)

        while not self._stop_recording.is_set():
            try:
                samples = int(self._chunk_duration * self._sample_rate)
                mic_audio = None
                sys_audio = None

                # ── Record mic + system audio in parallel ─────────
                if loopback_id is not None:
                    # Use threads to record both simultaneously
                    mic_buf = {"data": None}
                    sys_buf = {"data": None}

                    def record_mic():
                        try:
                            mic_buf["data"] = sd.rec(
                                samples, samplerate=self._sample_rate,
                                channels=1, dtype="float32",
                            )
                            sd.wait()
                        except Exception as e:
                            logger.debug(f"Mic record error: {e}")

                    def record_sys():
                        try:
                            # Use a separate InputStream for system audio
                            buf = np.zeros((samples, 1), dtype="float32")
                            pos = [0]
                            def callback(indata, frames, time_info, status):
                                end = min(pos[0] + frames, samples)
                                n = end - pos[0]
                                buf[pos[0]:end] = indata[:n]
                                pos[0] = end
                            with sd.InputStream(
                                device=loopback_id, samplerate=self._sample_rate,
                                channels=1, dtype="float32", callback=callback
                            ):
                                # Wait for recording to complete or stop signal
                                start = time.time()
                                while pos[0] < samples and not self._stop_recording.is_set():
                                    time.sleep(0.05)
                                    if time.time() - start > self._chunk_duration + 2:
                                        break
                            sys_buf["data"] = buf[:pos[0]]
                        except Exception:
                            pass  # Loopback not available

                    mic_thread = threading.Thread(target=record_mic, daemon=True)
                    sys_thread = threading.Thread(target=record_sys, daemon=True)
                    mic_thread.start()
                    sys_thread.start()

                    # Wait for both to complete (or stop signal)
                    for _ in range(self._chunk_duration * 10 + 20):
                        if self._stop_recording.is_set():
                            try: sd.stop()
                            except: pass
                            break
                        if not mic_thread.is_alive() and not sys_thread.is_alive():
                            break
                        time.sleep(0.1)
                    mic_thread.join(timeout=2)
                    sys_thread.join(timeout=2)

                    mic_audio = mic_buf["data"]
                    sys_audio = sys_buf["data"]
                    if mic_audio is not None:
                        mic_audio = mic_audio.flatten()
                    if sys_audio is not None:
                        sys_audio = sys_audio.flatten()
                else:
                    # No loopback — mic only
                    audio_data = sd.rec(
                        samples, samplerate=self._sample_rate,
                        channels=1, dtype="float32",
                    )
                    for _ in range(self._chunk_duration * 10):
                        if self._stop_recording.is_set():
                            sd.stop()
                            break
                        time.sleep(0.1)
                    else:
                        sd.wait()
                    mic_audio = audio_data.flatten() if audio_data is not None else None

                # ── Transcribe mic and system audio separately ────
                # Keeps [You] / [Others] labels — accurate with earphones.
                # Silent chunks are skipped; the call's end comes from call
                # detection, not from silence.
                if mic_audio is not None and len(mic_audio) > self._sample_rate:
                    self._transcribe_chunk(self._normalize_audio(mic_audio), speaker="You")
                if sys_audio is not None and len(sys_audio) > self._sample_rate:
                    self._transcribe_chunk(self._normalize_audio(sys_audio), speaker="Others")

            except Exception as e:
                logger.error(f"Recording error: {e}")
                if not self._stop_recording.is_set():
                    time.sleep(2)

    @staticmethod
    def _find_loopback_device(sd):
        """Find WASAPI loopback or Stereo Mix device for system audio."""
        try:
            devices = sd.query_devices()
            for i, d in enumerate(devices):
                name = d.get("name", "").lower()
                if ("loopback" in name or "stereo mix" in name) \
                        and d.get("max_input_channels", 0) > 0:
                    logger.info(f"Found system audio device: {d['name']}")
                    return i
        except Exception:
            pass
        logger.info("No system audio loopback found — mic only")
        return None

    @staticmethod
    def _normalize_audio(audio: np.ndarray) -> np.ndarray:
        """
        Normalize audio volume for consistent transcription input.
        Prevents issues with very quiet or very loud recordings.
        """
        if audio is None or len(audio) == 0:
            return audio
        peak = np.max(np.abs(audio))
        if peak > 0.001:  # Not silence
            audio = audio / peak * 0.85  # Normalize to 85% of max
        return audio

    def _transcribe_chunk(self, audio_data: np.ndarray, speaker: str = "You") -> bool:
        """Transcribe a single audio chunk with Gemma 4's audio encoder.
        Returns True if speech was detected and transcribed."""
        if len(audio_data) < self._sample_rate:
            return False  # Too short

        try:
            # Check if audio has actual content (not silence)
            rms = np.sqrt(np.mean(audio_data ** 2))
            if rms < 0.003:  # Silence threshold
                return False

            # Convert float32 numpy array to WAV bytes for Gemma
            audio_int16 = (audio_data * 32767).astype(np.int16)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(self._sample_rate)
                wf.writeframes(audio_int16.tobytes())
            wav_bytes = buf.getvalue()

            from screenmind.engine import llm_client
            text = llm_client.transcribe_audio(
                audio_bytes=wav_bytes,
                prompt="Transcribe this audio accurately. Output only the transcription, nothing else.",
                audio_format="wav",
                temperature=0.1,
                max_tokens=1024,
            )

            text = text.strip() if text else ""
            if text and len(text) > 5:  # Ignore very short fragments
                self._session_transcript.append(f"[{speaker}] {text}")
                logger.info(f"[{speaker}] {text[:100]}{'...' if len(text) > 100 else ''}")
                return True
            return False
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return False

    def _generate_summary(self, meeting_id: int, transcript: str):
        """Generate a structured meeting summary using Gemma.
        Uses map-reduce for long transcripts: chunk → summarize each → combine.
        """
        try:
            from screenmind.engine import llm_client

            FINAL_PROMPT_TEMPLATE = """You are a meeting notes assistant. Summarize this meeting content into structured sections.
Be concise and actionable. Extract the key information.

{content}

Generate a structured summary with these exact sections:

TOPICS DISCUSSED:
- topic 1
- topic 2

PROBLEMS RAISED:
- problem (if any)

SOLUTIONS PROPOSED:
- solution (if any)

ACTION ITEMS:
- [Speaker] specific action item

If a section has no content, write "None discussed."
"""

            if len(transcript) <= 4000:
                # Short transcript — single call
                prompt = FINAL_PROMPT_TEMPLATE.format(content=f"Meeting transcript:\n{transcript}")
                summary = llm_client.generate(prompt=prompt, temperature=0.3, max_tokens=1024)
            else:
                # Long transcript — map-reduce
                # Step 1: chunk transcript into ~3000 char segments
                chunk_size = 3000
                chunks = []
                for i in range(0, len(transcript), chunk_size):
                    chunks.append(transcript[i:i + chunk_size])

                logger.info(f"Long transcript ({len(transcript)} chars) — "
                      f"map-reduce with {len(chunks)} chunks")

                # Step 2: summarize each chunk
                chunk_summaries = []
                for idx, chunk in enumerate(chunks):
                    chunk_prompt = (
                        f"Summarize this portion ({idx + 1}/{len(chunks)}) of a meeting transcript. "
                        f"Extract key topics, decisions, and action items. Be concise.\n\n"
                        f"Transcript segment:\n{chunk}"
                    )
                    try:
                        chunk_summary = llm_client.generate(
                            prompt=chunk_prompt, temperature=0.3, max_tokens=512,
                        )
                        if chunk_summary and chunk_summary.strip():
                            chunk_summaries.append(f"--- Part {idx + 1} ---\n{chunk_summary.strip()}")
                            logger.info(f"Chunk {idx + 1}/{len(chunks)} summarized")
                    except Exception as e:
                        logger.warning(f"Chunk {idx + 1} summary failed: {e}")
                        chunk_summaries.append(f"--- Part {idx + 1} ---\n(Summary failed)")

                # Step 3: combine chunk summaries into final structured summary
                combined = "\n\n".join(chunk_summaries)
                prompt = FINAL_PROMPT_TEMPLATE.format(
                    content=f"Combined meeting summaries:\n{combined[:4000]}"
                )
                summary = llm_client.generate(prompt=prompt, temperature=0.3, max_tokens=1024)

            if summary and summary.strip():
                self._db.update_meeting_summary(meeting_id, summary.strip())
                logger.info(f"Meeting summary generated ({len(summary)} chars)")
            else:
                logger.warning("Empty summary from Gemma")
        except Exception as e:
            logger.error(f"Summary generation failed: {e}")
            self._db.update_meeting_summary(
                meeting_id, f"❌ Summary failed: {str(e)[:100]}"
            )

    def force_stop(self):
        """End the current call now (e.g., on shutdown or delete)."""
        if self._in_meeting:
            self._stop_meeting()

    @property
    def stats(self) -> dict:
        return {
            "available": self._available,
            "enabled": settings.meeting_transcription,
            "tracking": bool(settings.meeting_apps_list),
            "in_meeting": self._in_meeting,
            "recording": self._recording,
            "meeting_app": self._meeting_app,
            "meeting_title": self._meeting_title,
            "meeting_url": self._meeting_url,
            "transcript_chunks": len(self._session_transcript),
        }
