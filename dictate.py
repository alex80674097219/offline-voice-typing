# -*- coding: utf-8 -*-
"""
Ukrainian Voice Typing for Windows - live dictation in uk / ru / en / de.

Double-tap Ctrl (or Num Lock / Scroll Lock / Pause) toggles dictation ON/OFF.
While ON the script listens continuously, cuts speech into phrases at natural
pauses, transcribes each one and pastes it into whatever field has focus - so
text appears while you keep talking. A small always-on-top badge shows state.

https://github.com/alex80674097219/ukrainian-voice-typing
"""

import base64
import ctypes
import io
import os
import queue
import re
import sys
import threading
import time
import wave
import winsound
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
import keyboard
import pyperclip
import tkinter as tk

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "dictate.log"
KEY_FILE = BASE_DIR / "api_key.txt"
SETTINGS_FILE = BASE_DIR / "settings.txt"
LAST_WAV = BASE_DIR / "last_phrase.wav"
REPL_FILE = BASE_DIR / "replacements.txt"

_settings = {}
if SETTINGS_FILE.exists():
    for _raw in SETTINGS_FILE.read_text(encoding="utf-8").splitlines():
        _line = _raw.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _settings[_k.strip().upper()] = _v.strip()


def cfg(name, default):
    return _settings.get(name, os.environ.get("GD_" + name, default))


# ENGINE: "local" = Parakeet on this PC, no network (primary since 13.09.2026),
#         "stt"   = dedicated cloud speech-to-text via OpenRouter (reserve),
#         "chat"  = Gemini via chat completions (old fallback).
ENGINE = cfg("ENGINE", "local").lower()
STT_MODEL = cfg("STT_MODEL", "openai/whisper-large-v3-turbo")
CHAT_MODEL = cfg("CHAT_MODEL", "google/gemini-2.5-flash")
# Локальна модель. Заміряно 13.09.2026 на 40 архівних фразах, i9-12900F:
#   8 потоків: медіана 0.16 с, p95 0.34 с, найгірша (11 с мови) 0.48 с,
#   RAM +730 МБ. 14 потоків ПОВІЛЬНІШЕ (0.34 с) - E-ядра гальмують.
LOCAL_MODEL = cfg("LOCAL_MODEL", "nemo-parakeet-tdt-0.6b-v3")
LOCAL_QUANT = cfg("LOCAL_QUANT", "int8")
LOCAL_THREADS = int(cfg("LOCAL_THREADS", "8"))
# Якщо локальна модель впала або ще вантажиться - фраза йде у хмару.
LOCAL_FALLBACK = cfg("LOCAL_FALLBACK", "1").strip().lower() not in ("0", "no", "")
MODELS_DIR = BASE_DIR / "models"
# Числівники словами -> цифри ("сім тисяч п'ятсот" -> 7500). Локальна модель
# пише числа словами; правила і винятки - у numwords.py. 0 = вимкнути.
NUMBERS = cfg("NUMBERS", "1").strip().lower() not in ("0", "no", "")
try:
    import numwords as _numwords
except Exception:
    _numwords = None
# Empty LANGUAGE = auto-detect per phrase. ALLOWED_LANGS is the guard: if
# the model reports a language Alex does not dictate in, the phrase is
# transcribed again as FALLBACK_LANG instead of producing e.g. Belarusian.
LANGUAGE = cfg("LANGUAGE", "").strip()
ALLOWED_LANGS = {x.strip().lower()
                 for x in cfg("ALLOWED_LANGS", "uk,ru,en,de").split(",")
                 if x.strip()}
FALLBACK_LANG = cfg("FALLBACK_LANG", "uk").strip()
LANG_MAP = {
    "ukrainian": "uk", "russian": "ru", "english": "en", "german": "de",
    "belarusian": "be", "polish": "pl", "bulgarian": "bg",
    "serbian": "sr", "macedonian": "mk", "czech": "cs", "slovak": "sk",
    "croatian": "hr", "dutch": "nl", "kazakh": "kk", "romanian": "ro",
}
HOTKEY = cfg("HOTKEY", "num lock, scroll lock, pause")
# Keys listed here are swallowed so the focused app never sees them.
SUPPRESS = cfg("SUPPRESS", "num lock, scroll lock")
# Double-tap of this modifier toggles dictation. Empty = disabled.
DOUBLE_TAP = cfg("DOUBLE_TAP", "ctrl").strip().lower()
TAP_GAP = float(cfg("TAP_GAP", "0.45"))
TAP_MAX_HOLD = float(cfg("TAP_MAX_HOLD", "0.40"))
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
# Provider preference. The 12-22 s outliers in the log came from routing to a
# cold provider; Groq serves whisper fastest, DeepInfra stays as fallback.
PROVIDER_ORDER = [p.strip() for p in cfg("PROVIDER_ORDER", "Groq,DeepInfra")
                  .split(",") if p.strip()]
# If a request has not answered within this many seconds, fire a duplicate in
# parallel and use whichever comes back first. Kills the long tail.
HEDGE_AFTER = float(cfg("HEDGE_AFTER", "2.5"))
LANG_HOTKEY = cfg("LANG_HOTKEY", "ctrl+alt+l")
LANG_CYCLE = [x.strip() for x in
              cfg("LANG_CYCLE", ",uk,ru,de,en").split(",")]

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK = 512
BLOCK_SEC = BLOCK / float(SAMPLE_RATE)

SILENCE_TAIL = float(cfg("SILENCE_TAIL", "0.45"))
# Пауза на подумати - не кінець речення. Фраза йде на розпізнавання
# одразу після SILENCE_TAIL, але крапку ставимо лише якщо мовчання
# протривало довше за JOIN_WINDOW. Розпізнавання встигає за цей час,
# тому затримка не зростає.
JOIN_WINDOW = float(cfg("JOIN_WINDOW", "1.1"))
# Автовимкнення після тиші: щоб увімкнений мікрофон не ловив чужу
# розмову і не вставляв її у відкритий чат. 0 = вимкнути.
IDLE_OFF = float(cfg("IDLE_OFF", "120"))
IDLE_WARN = float(cfg("IDLE_WARN", "10"))
# How many phrases may be transcribed at the same time. Output order is
# always preserved - this only overlaps the network waits.
MAX_PARALLEL = int(cfg("MAX_PARALLEL", "3"))
PASTE_DELAY = float(cfg("PASTE_DELAY", "0.05"))
MIN_PHRASE = float(cfg("MIN_PHRASE", "0.7"))
MAX_PHRASE = 25.0
PRE_ROLL_SEC = 0.30
# Поріг тиші раніше був абсолютним (0.030). Це працювало, поки мікрофон
# давав пік 0.10-0.18. Коли рівень входу впав утричі (сесія 13.09: медіана
# піку 0.036, підсилення вперлося в стелю x12 у 72% фраз), той самий поріг
# почав з'їдати справжні слова: 24 відкидання з піками 0.013-0.028 при
# шумі в кімнаті 0.0006 - тобто у 20-45 разів гучніше за шум.
# Тепер поріг рахується від виміряного шуму: у тихій кімнаті пропускає
# тиху мову, у гучній - навпаки суворіший за старі 0.030.
MIN_PEAK = float(cfg("MIN_PEAK", "0.010"))       # жорстка нижня межа
PEAK_SNR = float(cfg("PEAK_SNR", "8.0"))         # пік фрази / рівень шуму
MIN_VOICED = float(cfg("MIN_VOICED", "0.45"))
ABS_FLOOR = float(cfg("ABS_FLOOR", "0.004"))
SNR_FACTOR = float(cfg("SNR_FACTOR", "3.5"))
TARGET_PEAK = 0.5
MAX_GAIN = float(cfg("MAX_GAIN", "25.0"))

# Whisper-family models emit these on silence/noise instead of nothing.
FILLERS = {
    "дякую", "дякую!", "дякую.", "дякуємо", "спасибо", "спасибо.",
    "спасибо!", "спасибо за просмотр", "спасибо за внимание",
    "thank you", "thank you.", "thanks", "thanks for watching",
    "so", "so.", "you", "bye", "bye.", "продовження далі", "субтитри",
    "субтитры", "редактор субтитров", "так", "так.", "да", "да.",
    "ага", "угу", "the end", "аминь", "amen",
}

TRANSCRIBE_PROMPT = (
    "Transcribe the audio verbatim, in the same language it is spoken. "
    "Never translate. Never continue, complete, summarise or answer what "
    "the speaker says. Output only words that are actually audible in the "
    "recording - if you are unsure, output nothing. Add natural punctuation. "
    "No labels, no comments, no quotation marks."
)

_active = False
_stream = None
_pasting = threading.Event()
_state_lock = threading.Lock()
_audio_q = queue.Queue()
_tx_q = queue.Queue()
_out_q = queue.Queue()
_last_lang = ["?"]
_forced_lang = [LANGUAGE]
_cont = {}          # seq -> True, якщо мова поновилася швидко
_cont_lock = threading.Lock()
_seq = [0]
_last_voice = [0.0]  # коли востаннє прийняли справжню фразу
_quiet_run = [0]     # скільки фраз поспіль прийшли з тихого мікрофона
_peak_ref = [0.12]   # типовий пік справжньої мови на цьому мікрофоні
_stats = {"ok": 0, "drop": 0, "took": [], "peak": []}  # зведення за сесію
_ui = None


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    try:
        print(line)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def beep(kind):
    try:
        if kind == "on":
            winsound.Beep(1000, 90)
            winsound.Beep(1400, 90)
        elif kind == "off":
            winsound.Beep(700, 90)
            winsound.Beep(450, 90)
        elif kind == "error":
            winsound.Beep(300, 400)
    except Exception:
        pass


class Badge:
    """Small always-on-top status window that never takes focus."""

    COLORS = {
        "off": ("#3a3a3a", "#bdbdbd"),
        "idle": ("#1f5130", "#9ae6b4"),
        "speech": ("#7a1f1f", "#ffd0d0"),
        "work": ("#7a5a1f", "#ffe9b0"),
        "error": ("#7a1f1f", "#ffd0d0"),
        "fade": ("#2e3138", "#8b93a7"),
    }

    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.92)
        self.label = tk.Label(
            self.win, text="", font=("Segoe UI", 11, "bold"),
            padx=14, pady=7, bg="#3a3a3a", fg="#bdbdbd",
        )
        self.label.pack()
        self.win.update_idletasks()
        self._no_activate()
        self.win.withdraw()

    def _no_activate(self):
        """WS_EX_NOACTIVATE - the badge must never steal keyboard focus."""
        try:
            hwnd = self.win.winfo_id()
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                hwnd = parent
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception as exc:
            log("WARNING no_activate: %s" % exc)

    def _place(self):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w = self.win.winfo_width()
        h = self.win.winfo_height()
        self.win.geometry("+%d+%d" % (sw - w - 24, sh - h - 90))

    def _apply(self, state, text, visible, alpha=None):
        try:
            bg, fg = self.COLORS.get(state, self.COLORS["idle"])
            self.label.configure(text=text, bg=bg, fg=fg)
            self.win.configure(bg=bg)
            self.win.attributes("-alpha", 0.92 if alpha is None
                                else max(0.15, min(0.92, alpha)))
            if visible:
                self.win.deiconify()
                self.win.attributes("-topmost", True)
                self._place()
            else:
                self.win.withdraw()
        except Exception:
            pass

    def set(self, state, text, visible=True, alpha=None):
        try:
            self.root.after(0, self._apply, state, text, visible, alpha)
        except Exception:
            pass

    def hide(self):
        self.set("off", "", False)

    def run(self):
        self.root.mainloop()


def ui(state, text, visible=True, alpha=None):
    if _ui is not None:
        _ui.set(state, text, visible, alpha)


def get_api_key():
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key and not key.startswith("PASTE"):
        return key
    if KEY_FILE.exists():
        for raw in KEY_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                return line
    return ""


def wav_bytes(audio):
    audio_int16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def _headers():
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("no OpenRouter API key")
    return {"Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "X-Title": "Ukrainian Voice Typing"}


def _make_session():
    """One reused connection instead of a fresh TLS handshake per phrase.
    Measured on 6 real phrases: 1.34s -> 0.55s median. The single largest
    speed win in this whole program."""
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=4,
                                            pool_maxsize=8, max_retries=0)
    s.mount("https://", adapter)
    return s


_session = _make_session()


def _post(url, payload):
    resp = _session.post(url, headers=_headers(), json=payload, timeout=90)
    if resp.status_code >= 400:
        raise RuntimeError("HTTP %s: %s" % (resp.status_code, resp.text[:300]))
    return resp.json()


def _post_hedged(url, payload, alt_payload=None):
    """Send the request; if it is still silent after HEDGE_AFTER seconds,
    send a second one in parallel - routed to the OTHER provider - and use
    whichever answers first. A duplicate costs a fraction of a cent; a 20
    second stall costs the whole point of dictating."""
    if HEDGE_AFTER <= 0:
        return _post(url, payload)
    done = threading.Event()
    box = {}
    state = {"launched": 1, "failed": 0}
    lock = threading.Lock()

    def attempt(tag):
        try:
            body = alt_payload if (tag == "hedge" and alt_payload) else payload
            data = _post(url, body)
            with lock:
                if "data" not in box:
                    box["data"] = data
                    box["tag"] = tag
            done.set()
        except Exception as exc:
            with lock:
                box.setdefault("exc", exc)
                state["failed"] += 1
                if state["failed"] >= state["launched"]:
                    done.set()

    threading.Thread(target=attempt, args=("first",), daemon=True).start()
    if not done.wait(HEDGE_AFTER):
        with lock:
            state["launched"] += 1
        log("slow provider (>%.1fs) - sending a parallel duplicate" % HEDGE_AFTER)
        threading.Thread(target=attempt, args=("hedge",), daemon=True).start()
    if not done.wait(95):
        raise RuntimeError("no answer from the API")
    if "data" in box:
        return box["data"]
    raise box.get("exc") or RuntimeError("request failed")


def _norm_lang(value):
    v = (value or "").strip().lower()
    return LANG_MAP.get(v, v)


def transcribe_stt(data):
    """Dedicated ASR model - structurally unable to invent or translate.

    With LANGUAGE empty the model detects the language of each phrase on its
    own, so Russian stays Russian and Ukrainian stays Ukrainian. If it lands
    on a language Alex never dictates in, the phrase is redone once as
    FALLBACK_LANG."""
    forced = _forced_lang[0]
    payload = {
        "model": STT_MODEL,
        "input_audio": {"data": base64.b64encode(data).decode("utf-8"),
                        "format": "wav"},
        "temperature": 0,
        "response_format": "verbose_json",
    }
    if PROVIDER_ORDER:
        payload["provider"] = {"order": PROVIDER_ORDER,
                               "allow_fallbacks": True}
    if forced:
        payload["language"] = forced
    alt = None
    if len(PROVIDER_ORDER) > 1:
        alt = dict(payload)
        alt["provider"] = {"order": list(reversed(PROVIDER_ORDER)),
                           "allow_fallbacks": True}
    result = _post_hedged(STT_URL, payload, alt)
    text = (result.get("text") or "").strip()
    detected = _norm_lang(result.get("language"))
    if not forced and ALLOWED_LANGS and detected \
            and detected not in ALLOWED_LANGS:
        log("language %r is outside %s - redoing as %s"
            % (detected, sorted(ALLOWED_LANGS), FALLBACK_LANG))
        payload["language"] = FALLBACK_LANG
        result = _post_hedged(STT_URL, payload, alt)
        text = (result.get("text") or "").strip()
        detected = FALLBACK_LANG
    _last_lang[0] = detected or "?"
    return text


def transcribe_chat(data):
    payload = {
        "model": CHAT_MODEL,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": TRANSCRIBE_PROMPT},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(data).decode("utf-8"),
                    "format": "wav"}},
            ],
        }],
    }
    return (_post(CHAT_URL, payload)["choices"][0]["message"]["content"]
            or "").strip()


# ---------------------------------------------------------------- local ---
# Parakeet TDT 0.6B v3 через onnxruntime, лише CPU (відеокарта AMD, CUDA
# немає). Модель вантажиться один раз у фоні при старті (~2 с); поки її
# нема - фрази йдуть у хмару. Розпізнавання серіалізоване через lock: при
# 0.16 с на фразу паралелити нема сенсу, а два прогони одночасно лише
# б'ються за ті самі ядра.
_local = {"model": None, "error": None, "lock": threading.Lock()}
_last_engine = ["cloud"]


def _load_local_model():
    t0 = time.time()
    try:
        MODELS_DIR.mkdir(exist_ok=True)
        os.environ.setdefault("HF_HOME", str(MODELS_DIR))
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        import onnxruntime as ort
        import onnx_asr
        so = ort.SessionOptions()
        so.intra_op_num_threads = LOCAL_THREADS
        so.inter_op_num_threads = 1
        model = onnx_asr.load_model(LOCAL_MODEL, quantization=LOCAL_QUANT,
                                    sess_options=so)
        # Прогрів: перший виклик завжди довший, хай він буде не на живій фразі.
        model.recognize(np.zeros(SAMPLE_RATE, dtype=np.float32),
                        sample_rate=SAMPLE_RATE)
        _local["model"] = model
        log("local model ready: %s (%s, %d threads) in %.1f s"
            % (LOCAL_MODEL, LOCAL_QUANT, LOCAL_THREADS, time.time() - t0))
    except Exception as exc:
        _local["error"] = exc
        log("ERROR local model failed to load: %s" % exc)
        log(traceback.format_exc().rstrip())
        if LOCAL_FALLBACK:
            log("all phrases will go to the cloud (%s)" % STT_MODEL)
            ui("show", "локальна модель не завантажилась - працюю через хмару",
               True)
            threading.Timer(4.0, lambda: _ui and _ui.hide()).start()


def _guess_lang(text):
    """Parakeet не повідомляє мову. Для плашки і для lower_first достатньо
    грубої оцінки за літерами."""
    if re.search(r"[іїєґІЇЄҐ]", text):
        return "uk"
    if re.search(r"[ыэъёЫЭЪЁ]", text):
        return "ru"
    if re.search(r"[А-Яа-я]", text):
        # Українське речення майже завжди має і/ї/є; кирилиця з "и" без них
        # на 4+ словах - це російська.
        if re.search(r"[иИ]", text) and len(text.split()) >= 4:
            return "ru"
        return "uk/ru"
    low = text.lower()
    if re.search(r"[äöüß]", low) or re.search(
            r"\b(und|ich|nicht|ist|das|die|der|wir|mit|für)\b", low):
        return "de"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return "?"


def transcribe_local(data):
    model = _local["model"]
    if model is None:
        raise RuntimeError(_local["error"] or "model still loading")
    with wave.open(io.BytesIO(data)) as wf:
        sr = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    with _local["lock"]:
        text = (model.recognize(audio, sample_rate=sr) or "").strip()
    _last_lang[0] = _guess_lang(text)
    return text


def transcribe(data):
    try:
        LAST_WAV.write_bytes(data)
    except Exception:
        pass
    if ENGINE == "chat":
        _last_engine[0] = "chat"
        return transcribe_chat(data)
    if ENGINE == "local":
        # Примусова мова (Ctrl+Alt+L) локальній моделі недоступна -
        # такі фрази свідомо йдуть у хмару. Це і є шлях для німецької.
        if not _forced_lang[0]:
            try:
                text = transcribe_local(data)
                _last_engine[0] = "local"
                return text
            except Exception as exc:
                if not LOCAL_FALLBACK:
                    raise
                log("local engine failed (%s) - cloud fallback" % exc)
    _last_engine[0] = "cloud"
    return transcribe_stt(data)


def paste(text):
    _pasting.set()
    try:
        try:
            old = pyperclip.paste()
        except Exception:
            old = None
        for mod in ("ctrl", "alt", "shift", "windows"):
            try:
                keyboard.release(mod)
            except Exception:
                pass
        # Буфер обміну в Windows - спільний ресурс: поки інша програма
        # (скріншот, Telegram, браузер) його тримає, OpenClipboard падає.
        # 13.09 так втратилося 5 фраз поспіль. Тепер - до 8 спроб за ~0.4 с.
        last_exc = None
        for attempt in range(8):
            try:
                pyperclip.copy(text)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(0.05)
        if last_exc is not None:
            raise last_exc
        time.sleep(PASTE_DELAY)
        keyboard.send("ctrl+v")
        time.sleep(PASTE_DELAY * 2)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass
    finally:
        time.sleep(PASTE_DELAY)
        _pasting.clear()


def _load_replacements():
    """Словник власних назв: моделі нестабільні на брендах, а локальна
    заміна коштує нуль мілісекунд і нуль центів."""
    pairs = []
    if not REPL_FILE.exists():
        return pairs
    try:
        for raw in REPL_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            pat, rep = line.split("=", 1)
            pat, rep = pat.strip(), rep.strip()
            if not pat:
                continue
            # Між словами правила модель може поставити будь-що:
            # "Конфиг-эксперт. Точка Энерджи." - тому пробіл у правилі
            # означає "пробіли, крапки, коми або дефіси".
            words = [re.escape(w) for w in re.split(r"[\s\-]+", pat) if w]
            body = r"[\s.,\-–]+".join(words)
            rx = re.compile(r"(?<!\w)" + body + r"(?!\w)",
                            re.IGNORECASE | re.UNICODE)
            pairs.append((rx, rep))
    except Exception as exc:
        log("WARNING replacements.txt: %s" % exc)
    # Довші правила застосовуються першими: інакше "пайлон" спрацює
    # раніше за "пайлон тех" і залишить хвіст "тех".
    pairs.sort(key=lambda p: -len(p[0].pattern))
    return pairs


REPLACEMENTS = _load_replacements()


def fix_terms(text):
    for rx, rep in REPLACEMENTS:
        text = rx.sub(rep, text)
    return text


def is_filler(text, dur, peak):
    """A lone stock phrase on a short/quiet segment is a hallucination,
    not speech. Real deliberate 'Дякую' is louder and rarely alone."""
    clean = text.strip().strip('"«»').lower()
    # Whisper позначає немовні звуки як *breath*, *ahem*, [music], (кашель).
    # Це не текст, а ремарка субтитрів - ніколи не вставляти.
    if re.fullmatch(r"[\*\[\(][^\*\]\)]{1,40}[\*\]\)][\.\s]*", clean):
        return True
    if clean.rstrip(".!…") in ("продолжение следует", "продовження далі",
                               "to be continued", "fortsetzung folgt"):
        return True
    if clean not in FILLERS:
        return False
    # Поріг гучності теж відносний: при тихому мікрофоні фіксовані 0.07
    # вбивали справжні короткі слова ("Так", "Да").
    return dur < 2.0 or peak < 0.55 * _peak_ref[0]


def archive(data, text):
    """Keep the last phrases as WAV so models can be compared on real voice."""
    try:
        d = BASE_DIR / "phrases"
        d.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        (d / ("%s.wav" % stamp)).write_bytes(data)
        with open(d / "index.txt", "a", encoding="utf-8") as fh:
            fh.write("%s.wav\t%s\n" % (stamp, text))
        old = sorted(d.glob("*.wav"))[:-40]
        for f in old:
            f.unlink(missing_ok=True)
    except Exception:
        pass


def audio_callback(indata, frames, time_info, status):
    if _active:
        _audio_q.put(indata[:, 0].copy())


def join_previous(text):
    """Прибираємо крапку в кінці і робимо наступне слово малою літерою -
    бо це продовження думки, а не нове речення. Крапка з комою, знаки
    питання й оклику лишаються: вони змістовні."""
    text = text.rstrip()
    if text.endswith((".", "…")) and not text.endswith(("...", "!.", "?.")):
        text = text[:-1].rstrip()
    return text


def lower_first(text):
    """'Ми можемо' -> 'ми можемо', але 'Victron' і 'MPPT' не чіпаємо."""
    m = re.match(r"^(\w+)", text, re.UNICODE)
    if not m:
        return text
    word = m.group(1)
    if not word[0].isupper() or not word[1:].islower():
        return text
    if not re.match(r"^[А-Яа-яІіЇїЄєҐґЁё]", word):
        return text
    return word[0].lower() + text[1:]


def _do_transcribe(item):
    audio, dur, peak, voiced_sec, seq, cut_at = item
    gain = min(TARGET_PEAK / peak, MAX_GAIN) if peak > 0 else 1.0
    loud = np.clip(audio * gain, -1.0, 1.0)
    data = wav_bytes(loud)
    t0 = time.time()
    text = transcribe(data)
    took = time.time() - t0
    log("phrase %.1fs peak=%.4f gain=x%.1f %s=%.2fs lang=%s -> %s"
        % (dur, peak, gain, _last_engine[0], took, _last_lang[0],
           text or "(empty)"))
    _stats["ok"] += 1
    _stats["took"].append(took)
    _stats["peak"].append(peak)
    _peak_ref[0] = 0.9 * _peak_ref[0] + 0.1 * peak
    # Мікрофон став тихим - попередити вголос, а не мовчки гризти слова.
    # Один раз за сесію: 13.09 з порогом 0.05 попередження сипалося щохвилини,
    # хоча розпізнавання йшло нормально. Нормальний пік цього мікрофона на
    # 100% рівня входу - 0.03-0.08; справжня біда починається нижче 0.03.
    if peak < 0.03:
        _quiet_run[0] += 1
        if _quiet_run[0] == 3:
            log("WARNING: mic level is low (peak %.4f) - check Windows "
                "input volume for the microphone" % peak)
            ui("show", "тихий мікрофон - перевірте рівень входу", True)
    elif _quiet_run[0] < 3:
        _quiet_run[0] = 0
    if text and is_filler(text, dur, peak):
        log("drop: filler hallucination %r (%.1fs peak=%.4f)"
            % (text, dur, peak))
        return None, None, seq, cut_at
    if text:
        fixed = fix_terms(text)
        if fixed != text:
            log("terms: %r -> %r" % (text, fixed))
            text = fixed
        if NUMBERS and _numwords is not None:
            try:
                digits = _numwords.convert(text)
            except Exception as exc:
                log("numbers: error %s" % exc)
                digits = text
            if digits != text:
                log("numbers: %r -> %r" % (text, digits))
                text = digits
                # Другий прохід словника - для назв із цифрами, які з'являються
                # лише після перетворення: "Avante 99" -> "Avante99".
                again = fix_terms(text)
                if again != text:
                    log("terms: %r -> %r" % (text, again))
                    text = again
    return text, data, seq, cut_at


def dispatcher_worker():
    """Sends phrases to the API in parallel - network waits overlap."""
    pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL,
                              thread_name_prefix="stt")
    while True:
        item = _tx_q.get()
        if item is None:
            _out_q.put(None)
            continue
        ui("work", "... розпізнаю")
        _out_q.put(pool.submit(_do_transcribe, item))


def paster_worker():
    """Pastes results strictly in the order the phrases were spoken."""
    first = True
    joining = [False]   # попередню фразу лишили без крапки
    while True:
        fut = _out_q.get()
        if fut is None:
            first = True
            joining[0] = False
            continue
        try:
            text, data, seq, cut_at = fut.result()
            # Чекаємо, поки стане ясно: пауза на подумати чи кінець речення.
            # Розпізнавання вже відбулося, тож зазвичай чекати нема чого.
            wait = cut_at + JOIN_WINDOW - time.time()
            if wait > 0:
                time.sleep(min(wait, JOIN_WINDOW))
            with _cont_lock:
                continues = _cont.pop(seq, False)
            if text:
                if joining[0]:
                    text = lower_first(text)
                if continues:
                    text = join_previous(text)
                archive(data, text)
                # "P" + пауза + "дробь 16" -> "P/16", а не "P /16".
                glue = first or (joining[0] and text.startswith("/"))
                try:
                    paste(text if glue else " " + text)
                except Exception as exc:
                    # Текст уже розпізнано - не губити його мовчки.
                    log("ERROR paste failed (%s), text was: %s" % (exc, text))
                    ui("error", "не вдалося вставити - буфер зайнятий")
                    beep("error")
                    time.sleep(0.5)
                    continue
                first = False
                joining[0] = continues
        except Exception as exc:
            log("ERROR transcribe: %s" % exc)
            ui("error", "помилка розпізнавання")
            beep("error")
            time.sleep(1.0)
        finally:
            if _active and _out_q.empty():
                ui("idle", "* диктування")


def segmenter_worker():
    """Cuts the incoming stream into phrases at natural pauses."""
    floor = [None]
    last = [None]   # (seq, коли фразу відрізали) - для склейки після паузи
    preroll = deque(maxlen=max(1, int(PRE_ROLL_SEC / BLOCK_SEC)))
    phrase = []
    voiced = 0
    voiced_sec = 0.0
    silence = 0.0
    speaking = False

    def flush():
        nonlocal phrase, speaking, silence, voiced, voiced_sec
        if phrase:
            audio = np.concatenate(phrase)
            dur = len(audio) / float(SAMPLE_RATE)
            peak = float(np.abs(audio).max()) if audio.size else 0.0
            # Поріг відносний до шуму, а не фіксований: інакше падіння
            # рівня мікрофона мовчки з'їдає слова (див. коментар до PEAK_SNR).
            noise = floor[0] if floor[0] else 0.0
            gate = max(MIN_PEAK, noise * PEAK_SNR)
            if dur < MIN_PHRASE:
                _stats["drop"] += 1
                log("drop: too short %.2fs peak=%.4f" % (dur, peak))
            elif peak < gate:
                _stats["drop"] += 1
                log("drop: too quiet %.2fs peak=%.4f gate=%.4f noise=%.4f"
                    % (dur, peak, gate, noise))
            elif voiced_sec < MIN_VOICED:
                _stats["drop"] += 1
                log("drop: only %.2fs voiced in %.2fs peak=%.4f"
                    % (voiced_sec, dur, peak))
            else:
                _seq[0] += 1
                _last_voice[0] = time.time()
                last[0] = (_seq[0], time.time())
                _tx_q.put((audio, dur, peak, voiced_sec, _seq[0],
                           time.time()))
        phrase = []
        speaking = False
        silence = 0.0
        voiced = 0
        voiced_sec = 0.0

    while True:
        block = _audio_q.get()
        if block is None:
            flush()
            floor[0] = None
            preroll.clear()
            continue
        rms = float(np.sqrt(np.mean(block ** 2)))
        # Рівень шуму має падати швидко і зростати майже ніколи. Інакше
        # довга гучна мова сама задирає поріг, і короткі слова після неї
        # відкидаються як тиша - у логах це були втрачені сегменти
        # з піком 0.12 і навіть 0.30.
        if floor[0] is None:
            floor[0] = rms
        elif rms < floor[0]:
            floor[0] = 0.85 * floor[0] + 0.15 * rms
        else:
            floor[0] = 0.9995 * floor[0] + 0.0005 * rms
        thr = max(ABS_FLOOR, floor[0] * SNR_FACTOR)
        if not speaking:
            preroll.append(block)
            if rms > thr:
                voiced += 1
                if voiced >= 2:
                    speaking = True
                    # Мова поновилася швидко після попередньої фрази -
                    # значить то була пауза на подумати, а не крапка.
                    if last[0] and time.time() - last[0][1] < JOIN_WINDOW:
                        with _cont_lock:
                            _cont[last[0][0]] = True
                    last[0] = None
                    phrase = list(preroll)
                    preroll.clear()
                    silence = 0.0
                    ui("speech", "* говорите")
            else:
                voiced = 0
        else:
            phrase.append(block)
            if rms > thr:
                silence = 0.0
                voiced_sec += BLOCK_SEC
            else:
                silence += BLOCK_SEC
            dur = len(phrase) * BLOCK_SEC
            if silence >= SILENCE_TAIL or dur >= MAX_PHRASE:
                flush()
                if _active:
                    ui("work", "... розпізнаю")


def idle_watchdog():
    """Вимикає диктування після тиші. Увімкнений мікрофон - це ризик:
    чужа розмова поруч може потрапити у відкритий чат."""
    warned = [False]
    while True:
        time.sleep(0.5)
        if not _active or IDLE_OFF <= 0:
            warned[0] = False
            continue
        idle = time.time() - _last_voice[0]
        left = IDLE_OFF - idle
        if left <= 0:
            log("auto-off: %.0f s of silence" % IDLE_OFF)
            warned[0] = False
            stop_dictation()
        elif left <= IDLE_WARN:
            warned[0] = True
            # Плавно гасне: від 0.92 до 0.2 за останні секунди.
            k = max(0.0, min(1.0, left / IDLE_WARN))
            ui("fade", "вимикаюсь через %d с - скажіть щось" % int(left + 0.9),
               True, 0.2 + 0.72 * k)
        elif warned[0]:
            warned[0] = False
            ui("idle", "* диктування")


def start_dictation():
    global _active, _stream
    with _state_lock:
        if _active:
            return
        try:
            _stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                                     blocksize=BLOCK, callback=audio_callback)
            _stream.start()
        except Exception as exc:
            log("ERROR microphone: %s" % exc)
            ui("error", "нет микрофона")
            beep("error")
            _stream = None
            return
        _active = True
    _last_voice[0] = time.time()
    _quiet_run[0] = 0
    log("dictation ON")
    beep("on")
    ui("idle", "* диктування увімкнено")


def _log_session_summary():
    """Один рядок на сесію: сьогоднішню проблему з тихим мікрофоном було
    видно лише мені і лише після ручного розбору лога. Тепер - одразу."""
    s = _stats
    try:
        if s["ok"] or s["drop"]:
            took = sorted(s["took"]) or [0.0]
            peak = sorted(s["peak"]) or [0.0]
            drop_pct = 100.0 * s["drop"] / max(1, s["ok"] + s["drop"])
            log("session: %d phrases, %d dropped (%.0f%%), %s median %.2fs "
                "p95 %.2fs max %.2fs, voice peak median %.3f"
                % (s["ok"], s["drop"], drop_pct, _last_engine[0],
                   took[len(took) // 2], took[int(len(took) * 0.95)],
                   took[-1], peak[len(peak) // 2]))
            if peak[len(peak) // 2] < 0.03:
                log("session: voice is quiet - check the Windows input level")
    except Exception:
        pass
    s["ok"] = 0
    s["drop"] = 0
    s["took"] = []
    s["peak"] = []


def stop_dictation():
    global _active, _stream
    with _state_lock:
        if not _active:
            return
        _active = False
        try:
            _stream.stop()
            _stream.close()
        except Exception:
            pass
        _stream = None
    _audio_q.put(None)
    _tx_q.put(None)
    log("dictation OFF")
    _log_session_summary()
    beep("off")
    ui("off", "диктування вимкнено")
    threading.Timer(1.6, lambda: _ui and _ui.hide()).start()


# Num Lock / Scroll Lock also flip a Windows state we do not want to change.
# Blocking them with suppress=True breaks the whole keyboard listener, so
# instead we let the flip happen and immediately flip it back.
LOCK_KEYS = {"num lock": 0x90, "scroll lock": 0x91, "caps lock": 0x14}
_lock_guard = [0.0]


def _flip_lock(vk):
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
    except Exception as exc:
        log("WARNING restore lock state: %s" % exc)


def make_toggle(combo):
    vk = LOCK_KEYS.get(combo.strip().lower())
    if vk is None:
        return toggle

    def cb():
        now = time.time()
        if now < _lock_guard[0]:
            return
        _lock_guard[0] = now + 0.8
        threading.Timer(0.05, _flip_lock, args=(vk,)).start()
        toggle()

    return cb


def cycle_language():
    """Force one language for a while - useful for German, which is hard to
    auto-detect inside short Ukrainian/Russian sentences."""
    try:
        cur = _forced_lang[0]
        idx = LANG_CYCLE.index(cur) if cur in LANG_CYCLE else 0
        nxt = LANG_CYCLE[(idx + 1) % len(LANG_CYCLE)]
        _forced_lang[0] = nxt
        label = nxt.upper() if nxt else "АВТО"
        log("forced language -> %s" % (nxt or "auto"))
        ui("work", "мова: %s" % label)
        threading.Timer(
            1.8, lambda: ui("idle", "* диктування") if _active
            else (_ui and _ui.hide())).start()
        beep("ok")
    except Exception as exc:
        log("WARNING cycle_language: %s" % exc)


_tap = {"last": 0.0, "down": 0.0, "clean": False}


def install_double_tap(key):
    """Toggle on a double tap of a modifier (default Ctrl).

    A tap counts only when the key went down and up alone: if any other key
    was pressed while it was held, it was a shortcut (Ctrl+C, Ctrl+V) and is
    ignored. Our own synthetic Ctrl+V during pasting is ignored too."""
    names = {"ctrl": ("ctrl", "left ctrl", "right ctrl"),
             "shift": ("shift", "left shift", "right shift"),
             "alt": ("alt", "left alt", "right alt", "alt gr")}
    watched = names.get(key, (key,))

    def handler(event):
        try:
            _handle(event)
        except Exception as exc:
            log("WARNING double-tap handler: %s" % exc)

    def _handle(event):
        if _pasting.is_set():
            return
        name = (event.name or "").lower()
        hit = name in watched
        if event.event_type == "down":
            if hit:
                if _tap["down"] == 0.0:
                    _tap["down"] = time.time()
                    _tap["clean"] = True
            else:
                _tap["clean"] = False
                _tap["last"] = 0.0
        elif event.event_type == "up" and hit:
            held = time.time() - _tap["down"] if _tap["down"] else 99.0
            clean = _tap["clean"]
            _tap["down"] = 0.0
            _tap["clean"] = False
            if not clean or held > TAP_MAX_HOLD:
                _tap["last"] = 0.0
                return
            now = time.time()
            if 0.0 < now - _tap["last"] < TAP_GAP:
                _tap["last"] = 0.0
                toggle()
            else:
                _tap["last"] = now

    keyboard.hook(handler)


def toggle():
    if _active:
        threading.Thread(target=stop_dictation, daemon=True).start()
    else:
        threading.Thread(target=start_dictation, daemon=True).start()


_mutex = []


def _single_instance():
    """Два екземпляри = кожна фраза вставляється двічі. Іменований м'ютекс
    Windows живе, поки живий процес, тож після краху нічого чистити не треба."""
    try:
        k32 = ctypes.windll.kernel32
        handle = k32.CreateMutexW(None, False, "Local\\UkrainianVoiceTyping")
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _mutex.append(handle)
    except Exception as exc:
        log("WARNING single-instance check failed: %s" % exc)
    return True


def main():
    global _ui
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    log("=" * 50)
    if not _single_instance():
        log("another instance is already running - exiting")
        sys.exit(0)
    model_name = {"local": LOCAL_MODEL, "chat": CHAT_MODEL}.get(ENGINE, STT_MODEL)
    log("Dictation starting, engine=%s model=%s lang=%s"
        % (ENGINE, model_name, LANGUAGE or "auto"))
    if ENGINE == "local":
        log("cloud reserve: %s (used while the model loads, on failure, "
            "and for a forced language via %s)" % (STT_MODEL, LANG_HOTKEY))
        threading.Thread(target=_load_local_model, daemon=True).start()
    if not get_api_key():
        if ENGINE == "local":
            log("WARNING: no API key in %s - no cloud reserve" % KEY_FILE)
        else:
            log("ERROR: no API key in %s" % KEY_FILE)
            beep("error")
            sys.exit(1)
    try:
        log("input device: %s" % sd.query_devices(kind="input")["name"])
    except Exception as exc:
        log("WARNING input device: %s" % exc)

    threading.Thread(target=segmenter_worker, daemon=True).start()
    threading.Thread(target=dispatcher_worker, daemon=True).start()
    threading.Thread(target=paster_worker, daemon=True).start()
    threading.Thread(target=idle_watchdog, daemon=True).start()

    suppressed = {k.strip().lower() for k in SUPPRESS.split(",") if k.strip()}
    registered = []
    for combo in [c.strip() for c in HOTKEY.split(",") if c.strip()]:
        try:
            keyboard.add_hotkey(combo, make_toggle(combo),
                                suppress=combo.lower() in suppressed)
            registered.append(combo)
        except Exception as exc:
            log("WARNING hotkey %r not registered: %s" % (combo, exc))
    if not registered:
        log("ERROR: no hotkey could be registered")
        beep("error")
        sys.exit(1)
    log("hotkeys: %s" % ", ".join(registered))
    if LANG_HOTKEY:
        try:
            keyboard.add_hotkey(LANG_HOTKEY, cycle_language)
            log("language switch: %s (cycle: %s)"
                % (LANG_HOTKEY, " -> ".join(x or "auto" for x in LANG_CYCLE)))
        except Exception as exc:
            log("WARNING language hotkey: %s" % exc)
    if DOUBLE_TAP:
        try:
            install_double_tap(DOUBLE_TAP)
            log("double tap: %s" % DOUBLE_TAP)
        except Exception as exc:
            log("WARNING double tap %r failed: %s" % (DOUBLE_TAP, exc))

    _ui = Badge()
    log("ready")
    ui("off", "Диктування: %s" % registered[0])
    threading.Timer(3.0, lambda: _ui and _ui.hide()).start()
    _ui.run()


if __name__ == "__main__":
    main()
