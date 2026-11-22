#!/usr/bin/env python3
"""claude-voice-notify: spoken notifications for Claude Code.

One file, standard library only, Python 3.9+. Claude Code runs it as a hook:

  UserPromptSubmit  (sync)   asks Claude to end its reply with one "🔊 ..." line, in first person
  Stop              (async)  queues that line as it is: the agent speaks for itself
  Notification      (async)  queues "<name> needs you." when a permission prompt or question waits, with the
                             name the agent used in this session, or the repository name if it never gave one

and the /voice-notify command runs it with on, off, test, voices, status, lang, voice, command or mic.

On macOS it also watches the microphone (CoreAudio, through ctypes): while an app is listening to you it waits its
turn, and while you are in a call it shows a notification instead of speaking (see "microphone" below).

It speaks with the system's own engine: AVSpeech on macOS (reaches the Siri voices), speech-dispatcher or
eSpeak NG on Linux, System.Speech on Windows. Or with a program you choose (/voice-notify command).
Nothing leaves this machine unless that program sends it somewhere.
"""
import datetime
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("CLAUDE_VOICE_NOTIFY_HOME") or Path.home() / ".claude" / "voice-notify")
ENABLED = STATE / "enabled"      # exists = speak
CONFIG = STATE / "config.json"   # {"language": "auto" | "en" | "pt-BR", "voice": "" (auto) | name, "command": "",
#                                  "mic_aware": true, "mic_ignore": [...], "mic_calls": [...]}
QUEUE = STATE / "queue.jsonl"    # announcements waiting their turn, one JSON per line
LOG = STATE / "voice.log"        # last 50 hook events, including the silent ones
AGENTS = STATE / "agents.json"   # {session_id: {"article", "name", "at"}}: the name an agent used in that session
AGENTS_KEEP = 200
LOG_LINES = 50

LANGUAGES = ("en", "pt-BR")

# Microphone (macOS 14+). A process "has the input open" when CoreAudio says it is running input. Some always do.
MIC_IGNORE = ("com.apple.CoreSpeech", "historicalaudiod")   # always listening, whatever you do (bundle id or process name)
MIC_CALLS = ("com.hnc.Discord", "us.zoom.xos", "com.microsoft.teams2", "com.microsoft.teams", "com.apple.FaceTime",
             "com.tinyspeck.slackmacgap")   # a bundle id also matches its helpers: com.hnc.Discord.helper.Renderer
MIC_POLL = 0.5     # seconds between looks at the microphone while an announcement waits
MIC_SETTLE = 1.0   # the input must stay closed this long before a waiting announcement is spoken
MIC_WATCH = 0.4    # while speaking: how often to look at the microphone, so a sentence is cut soon after you start talking

# The line is written by the same Claude that did the work, so it costs nothing extra and knows what matters.
# It speaks as itself, in first person. The "this is <name>" / "aqui é o <nome>" opening is what the plugin
# reads to remember the agent's name for this session (see agent_from_summary).
INSTRUCTION = {
    "en": "Voice notifications are on (claude-voice-notify): at the end of your final reply this turn, if there is "
          "something the user needs to know, close with ONE line starting with 🔊 and 2 to 4 short sentences in "
          "English, written to be heard. Speak as yourself, in the first person, as if talking to the person: what "
          "you did and what you need from them (approve, review, decide). Open by introducing yourself. If you have "
          "a name of your own as an agent or persona (from the CLAUDE.md or your agent file), say \"this is <Name>.\" "
          "(\"Alex, this is Orion. I finished the landing page...\"); if you don't, say which project you are in "
          "(\"Alex, I finished the work on acme web...\"). Use the person's name if you know it. "
          "No code, file paths, markdown or links. If nothing is worth saying, leave the line out.",
    "pt-BR": "Voz ligada (claude-voice-notify): no fim da resposta final deste turno, se houver algo que a pessoa "
             "precise saber, feche com UMA linha começando com 🔊 e 2 a 4 frases curtas em português, escritas para "
             "serem ouvidas. Fale como você mesmo, em primeira pessoa, como se conversasse com a pessoa: o que fez e "
             "o que precisa dela (aprovar, olhar, decidir). Comece se apresentando. Se você tem nome próprio de "
             "agente ou persona (do CLAUDE.md ou do seu arquivo de agente), diga \"aqui é o <Nome>.\" ou \"aqui é a "
             "<Nome>.\" (\"Alex, aqui é o Orion. Terminei a landing...\"); se não tem, diga em que projeto está "
             "(\"Alex, terminei o trabalho no acme web...\"). Chame a pessoa pelo nome se souber. "
             "Sem código, caminho de arquivo, markdown nem link. Se não houver nada que valha falar, não escreva a linha.",
}

# How a person would tell you. With a 🔊 line, the agent's own words are spoken and nothing else. The rest is for what
# the agent did not write: a Stop without a 🔊 line, a permission prompt, a question. {p} is the agent's name
# ("O Orion", "Orion") when the session gave one, else the repository name (never the session title).
PHRASES = {
    "en": {
        "done": ("{p} is done.", "{p} finished.", "{p} is done, take a look when you can.", "All done in {p}."),
        "done_named": ("{p} is done.", "{p} finished.", "{p} is done, take a look when you can."),
        "approval": ("{p} needs you.", "{p} is waiting for your approval."),
        "question": ("{p} has a question for you.",),
        "on": "Voice on.",
        "test": "This is how I will sound when I have something to tell you.",
        "call_title": "{p} wants to talk to you",   # the notification shown instead of speaking, while you are in a call
    },
    "pt-BR": {
        "done": ("{p} terminou.", "Pronto, {p} acabou.", "{p} terminou, dá uma olhada quando puder.", "Terminei no {p}."),
        "done_named": ("{p} terminou.", "Pronto, {p} acabou.", "{p} terminou, dá uma olhada quando puder."),
        "approval": ("{p} precisa de você.", "{p} está esperando sua aprovação."),
        "question": ("{p} tem uma pergunta pra você.",),
        "on": "Voz ligada.",
        "test": "É assim que eu vou soar quando tiver algo pra te contar.",
        "call_title": "{p} quer falar com você",
    },
}

# macOS: AVSpeech through JavaScript for Automation reaches the Siri and premium voices; `say` does not.
SPEAK_JS = """ObjC.import("AVFoundation");
function run(argv) {
  const s = $.AVSpeechSynthesizer.alloc.init, u = $.AVSpeechUtterance.speechUtteranceWithString(argv[2]);
  let v = argv[0] ? $.AVSpeechSynthesisVoice.voiceWithIdentifier(argv[0]) : $();
  if (v.isNil()) v = $.AVSpeechSynthesisVoice.voiceWithLanguage(argv[1]);
  u.voice = v;
  s.speakUtterance(u); delay(0.3);
  while (s.speaking) delay(0.1);   // without this wait the process exits mid-sentence
}"""
VOICES_JS = """ObjC.import("AVFoundation");
const v = $.AVSpeechSynthesisVoice.speechVoices, r = [];
for (let i = 0; i < v.count; i++) { const x = v.objectAtIndex(i);
  r.push([x.identifier.js, x.name.js, x.language.js, x.quality].join("\\t")); }
r.join("\\n")"""
QUALITY = {1: "default", 2: "enhanced", 3: "premium"}

# Windows: System.Speech ships with Windows PowerShell 5.1 (powershell.exe; PowerShell 7 doesn't have it).
# The text and the voice travel in environment variables, so nothing is ever quoted into a command line.
SAPI_PS = """Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = $s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo }
$p = if ($env:VOICE_NOTIFY_VOICE) { $v | Where-Object { $_.Name -eq $env:VOICE_NOTIFY_VOICE } }
     else { $v | Where-Object { $_.Culture.Name -like "$($env:VOICE_NOTIFY_LANG)*" } }
if ($p) { $s.SelectVoice(@($p)[0].Name) }
$s.Speak($env:VOICE_NOTIFY_TEXT)"""
SAPI_VOICES_PS = """Add-Type -AssemblyName System.Speech
(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | Where-Object { $_.Enabled } |
  ForEach-Object { $_.VoiceInfo.Name + "`t" + $_.VoiceInfo.Name + "`t" + $_.VoiceInfo.Culture.Name + "`t1" }"""

ESPEAK_VOICE = {"en": "en-us", "pt-BR": "pt-br"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # Windows: no console window flashing up


def system():
    """"mac", "linux", "windows" or another sys.platform value."""
    if sys.platform == "darwin":
        return "mac"
    if os.name == "nt":
        return "windows"
    return "linux" if sys.platform.startswith("linux") else sys.platform


class Interrupted(Exception):
    """The microphone opened while something was being said: the speech was cut."""


def run_stoppable(cmd, timeout, stop, **popen):
    """Runs cmd and waits for it, looking at stop() every MIC_WATCH seconds. If stop() says True the process (and,
    on macOS and Linux, its whole group) is killed and Interrupted is raised. Returns the exit code."""
    group = os.name != "nt"
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW, start_new_session=group, **popen)
    except OSError:
        return 127
    deadline = time.monotonic() + timeout
    while True:
        try:
            return proc.wait(timeout=MIC_WATCH)
        except subprocess.TimeoutExpired:
            pass
        timed_out = time.monotonic() > deadline
        if timed_out or stop():
            try:
                os.killpg(proc.pid, 15) if group else proc.kill()   # 15 = SIGTERM
            except OSError:
                pass
            proc.wait()
            if timed_out:
                return 124
            raise Interrupted()


def run(cmd, cwd=None, timeout=10, env=None, stop=None):
    if stop:   # speech that can be cut: see run_stoppable
        return subprocess.CompletedProcess(cmd, run_stoppable(cmd, timeout, stop, cwd=cwd, env=env), "", "")
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


# --- configuration -------------------------------------------------------------------------------------------

def load_config():
    try:
        c = json.loads(CONFIG.read_text(encoding="utf-8"))
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(**changes):
    """Saves the changes; a value of None removes the key (back to the default)."""
    c = load_config()
    c.update(changes)
    c = {k: v for k, v in c.items() if v is not None}
    STATE.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(c, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def windows_ui_language():
    import ctypes
    import locale
    return locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")


def system_language():
    """The interface language (macOS AppleLanguages, Windows UI language), then the POSIX locale. "" if unknown."""
    try:
        if system() == "mac":
            m = re.search(r'\(\s*"?([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)*)',
                          run(["defaults", "read", "-g", "AppleLanguages"]).stdout)
            if m:
                return m.group(1)
        elif system() == "windows":
            tag = windows_ui_language()
            if tag:
                return tag
    except Exception:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var, "") not in ("", "C", "POSIX"):
            return os.environ[var]
    return ""


def language(config=None):
    lang = (config if config is not None else load_config()).get("language") or "auto"
    if lang in LANGUAGES:
        return lang
    return "pt-BR" if system_language().lower().startswith("pt") else "en"


# --- speech engines ------------------------------------------------------------------------------------------

def system_engine():
    """The system's own speech engine: "avspeech", "sapi", "spd-say", "espeak-ng", or "" if there is none."""
    s = system()
    if s == "mac":
        return "avspeech"
    if s == "windows":
        return "sapi" if shutil.which("powershell") else ""
    if s == "linux":
        return next((e for e in ("spd-say", "espeak-ng") if shutil.which(e)), "")
    return ""


def can_speak(config=None):
    return bool((config if config is not None else load_config()).get("command")) or bool(system_engine())


def installed_voices():
    """[(identifier, name, language, quality)] on macOS (AVSpeech) and Windows (System.Speech). [] elsewhere:
    on Linux the engine picks a voice for the language."""
    engine = system_engine()
    if engine == "avspeech":
        out = run(["osascript", "-l", "JavaScript", "-e", VOICES_JS]).stdout
    elif engine == "sapi":
        out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SAPI_VOICES_PS], timeout=30).stdout
    else:
        return []
    voices = []
    for line in out.splitlines():
        f = line.strip("\r").split("\t")
        if len(f) == 4:
            voices.append((f[0], f[1], f[2], int(f[3]) if f[3].isdigit() else 1))
    return voices


ODD_VOICES = re.compile(r"speech\.synthesis\.voice|eloquence|personalvoice")   # novelty, Eloquence, Personal Voice


def voice_rank(voice, lang):
    """Higher is better: Siri voices, then premium > enhanced > default, then the main region, then not compact."""
    ident, _, vlang, quality = voice
    main_region = {"en": "en-us", "pt-BR": "pt-br"}[lang]
    return (".siri." in ident, quality, vlang.lower() == main_region, not ODD_VOICES.search(ident),
            "compact" not in ident)


def best_voice(lang, voices):
    """The best installed voice for the language, or None. Novelty, Eloquence and Personal Voice are left out."""
    tag = lang.lower()
    usable = [v for v in voices if v[2].lower().startswith(tag) and not ODD_VOICES.search(v[0])]
    return max(usable, key=lambda v: voice_rank(v, lang)) if usable else None


def chosen_voice(lang, config=None):
    """The configured voice, or the best installed one for the language ("" lets the engine pick)."""
    v = (config if config is not None else load_config()).get("voice") or ""
    if v:
        return v
    best = best_voice(lang, installed_voices())
    return best[0] if best else ""


def speech_env(text, voice, lang):
    return dict(os.environ, VOICE_NOTIFY_TEXT=text, VOICE_NOTIFY_LANG=lang, VOICE_NOTIFY_VOICE=voice or "")


def run_command(command, text, voice, lang, stop=None):
    """Your own speech program. It runs in the system shell with the text in VOICE_NOTIFY_TEXT (plus _LANG and
    _VOICE); on macOS and Linux the text is also its last argument. True if it exited with 0. With `stop`, see
    run_stoppable (Interrupted is raised if it says True)."""
    env = speech_env(text, voice, lang)
    try:
        if system() == "windows":
            argv, shell = command, True
        else:
            argv, shell = ["/bin/sh", "-c", command + ' "$VOICE_NOTIFY_TEXT"'], False
        if stop:
            return run_stoppable(argv, 300, stop, shell=shell, env=env) == 0
        r = subprocess.run(argv, shell=shell, env=env, timeout=300, creationflags=NO_WINDOW,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def speak(text, voice, lang, command="", stop=None):
    """Says the text and returns when it's done. Your program goes first; if it fails, the system voice speaks.
    `stop` is a function that returns True when the speech must be cut: Interrupted is raised."""
    kw = {"stop": stop} if stop else {}
    if command and run_command(command, text, voice, lang, **kw):
        return
    engine = system_engine()
    if engine == "avspeech":
        # A dotted identifier goes through AVSpeech (reaches Siri), a short name through `say -v`.
        if voice and "." not in voice:
            r = run(["say", "-v", voice, text], timeout=180, **kw)
        else:
            r = run(["osascript", "-l", "JavaScript", "-e", SPEAK_JS, voice or "", lang, text], timeout=180, **kw)
        if r.returncode:
            run(["say", text], timeout=180, **kw)
    elif engine == "sapi":
        run(["powershell", "-NoProfile", "-NonInteractive", "-Command", SAPI_PS], timeout=180,
            env=speech_env(text, voice, lang), **kw)
    elif engine == "spd-say":   # -w waits until the sentence is spoken
        r = run(["spd-say", "-w", "-l", lang] + (["-y", voice] if voice else []) + [text], timeout=90, **kw)
        if r.returncode and shutil.which("espeak-ng"):   # no speech-dispatcher daemon or audio: try eSpeak NG
            run(["espeak-ng", "-v", ESPEAK_VOICE[lang], text], timeout=180, **kw)
    elif engine == "espeak-ng":
        run(["espeak-ng", "-v", voice or ESPEAK_VOICE[lang], text], timeout=180, **kw)

# --- microphone ----------------------------------------------------------------------------------------------
# Speaking over you is rude, and in a call it leaks into the other person's ears. macOS 14+ tells which processes
# have an audio input open (CoreAudio: kAudioHardwarePropertyProcessObjectList, then per process
# kAudioProcessPropertyIsRunningInput, ...PID and ...BundleID). The device-level "running somewhere" flag does not
# work for this: a few system daemons keep the input open all day, so it never goes to 0.
#
# Three states: "free" (speak as always), "busy" (an app is listening to you: wait, speak when it closes) and
# "call" (a call app listens: do not speak, show a notification). Any failure, or any other system, means "free":
# the voice behaves as it did before this existed, and a broken query can never hold the queue.

CA_SELECTORS = {"list": b"prs#", "input": b"piri", "pid": b"ppid", "bundle": b"pbid"}
CA_GLOBAL, CA_MAIN = b"glob", 0
CA_SYSTEM_OBJECT = 1
CF_UTF8 = 0x08000100


def fourcc(code):
    return int.from_bytes(code, "big")


PROCESS_NAMES = {}   # pid -> name: a name never changes, and asking costs a process


def process_name(pid):
    """The executable name of a process, for the ones that have no bundle id. "" if unknown. proc_name refuses
    processes of other users (historicalaudiod, for one), so `ps`, which may ask the kernel for any, is the fallback."""
    if pid not in PROCESS_NAMES:
        name = ""
        try:
            import ctypes
            buf = ctypes.create_string_buffer(256)
            if ctypes.CDLL("/usr/lib/libSystem.B.dylib").proc_name(int(pid), buf, 256) > 0:
                name = buf.value.decode("utf-8", "replace")
        except Exception:
            pass
        if not name:
            name = os.path.basename(run(["ps", "-p", str(int(pid)), "-o", "comm="], timeout=5).stdout.strip())
        PROCESS_NAMES[pid] = name
    return PROCESS_NAMES[pid]


def coreaudio_inputs():
    """[(pid, identity)] for every process that has an audio input open right now; identity is its bundle id, or
    its executable name when it has none. None if this can't be asked (not macOS, macOS before 14, any error)."""
    if system() != "mac":
        return None
    try:
        import ctypes
        import ctypes.util

        class Address(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]

        ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio") or "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation")
                         or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(Address), ctypes.c_uint32,
                                                      ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(Address), ctypes.c_uint32,
                                                  ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        def address(name):
            return Address(fourcc(CA_SELECTORS[name]), fourcc(CA_GLOBAL), CA_MAIN)

        def get(obj, name, ctype):
            value, size = ctype(), ctypes.c_uint32(ctypes.sizeof(ctype))
            if ca.AudioObjectGetPropertyData(obj, ctypes.byref(address(name)), 0, None, ctypes.byref(size),
                                             ctypes.byref(value)):
                return None
            return value.value

        size = ctypes.c_uint32(0)
        if ca.AudioObjectGetPropertyDataSize(CA_SYSTEM_OBJECT, ctypes.byref(address("list")), 0, None,
                                             ctypes.byref(size)):
            return None   # macOS before 14 has no process list
        ids = (ctypes.c_uint32 * (size.value // 4))()
        size = ctypes.c_uint32(ctypes.sizeof(ids))
        if ca.AudioObjectGetPropertyData(CA_SYSTEM_OBJECT, ctypes.byref(address("list")), 0, None,
                                         ctypes.byref(size), ctypes.byref(ids)):
            return None
        found = []
        for obj in ids[:size.value // 4]:
            if get(obj, "input", ctypes.c_uint32) != 1:   # also skips a process that quit while we were looking
                continue
            pid = get(obj, "pid", ctypes.c_int32)
            ref = get(obj, "bundle", ctypes.c_void_p)
            bundle = ""
            if ref:
                buf = ctypes.create_string_buffer(512)
                if cf.CFStringGetCString(ref, buf, 512, CF_UTF8):
                    bundle = buf.value.decode("utf-8", "replace")
                cf.CFRelease(ref)   # the property is a copy: it is ours to release
            found.append((pid or 0, bundle or process_name(pid) or "unknown"))
        return found
    except Exception:
        return None


def matches(identity, patterns):
    """True if identity is one of the patterns or lives under one ("com.hnc.Discord" matches
    "com.hnc.Discord.helper.Renderer"). Case does not matter."""
    ident = identity.lower()
    return any(ident == p.lower() or ident.startswith(p.lower() + ".") for p in patterns if p)


def mic_list(config, key, default):
    value = config.get(key)
    return [str(v) for v in value] if isinstance(value, list) else list(default)


def mic_enabled(config):
    return system() == "mac" and config.get("mic_aware") is not False


def mic_report(config=None):
    """(state, rows) for the microphone now. rows = [(pid, identity, role)], role "ignored", "call" or "busy";
    state is "call" if any row is a call, else "busy" if any is busy, else "free". None if it can't be asked."""
    config = load_config() if config is None else config
    inputs = coreaudio_inputs()
    if inputs is None:
        return None
    ignore, calls = mic_list(config, "mic_ignore", MIC_IGNORE), mic_list(config, "mic_calls", MIC_CALLS)
    rows = [(pid, ident, "ignored" if matches(ident, ignore) else "call" if matches(ident, calls) else "busy")
            for pid, ident in inputs]
    roles = {r[2] for r in rows}
    return ("call" if "call" in roles else "busy" if "busy" in roles else "free"), rows


def mic_state(config=None):
    """"free", "busy" or "call". Never raises, and never says anything but "free" when it can't tell."""
    try:
        config = load_config() if config is None else config
        if not mic_enabled(config):
            return "free"
        report = mic_report(config)
        return report[0] if report else "free"
    except Exception:
        return "free"


def notify(title, body):
    """A macOS notification (the text travels as arguments, never inside the script). True if it was posted."""
    if system() != "mac":
        return False
    script = ["on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run"]
    r = run(["osascript"] + [x for line in script for x in ("-e", line)] + [title, body])
    return r.returncode == 0


# --- queue ---------------------------------------------------------------------------------------------------
# A hook runs inside Claude Code's hook lifecycle, and two hooks speaking at once talk over each other.
# So the hook only appends to a queue and starts a detached speaker; one speaker at a time reads it in order.

class FileLock:
    """An exclusive lock on a file in STATE: flock on macOS and Linux, msvcrt on Windows."""

    def __init__(self, f):
        self.f = f

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()

    def release(self):
        if os.name == "nt":
            import msvcrt
            try:
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.f.close()   # closing also releases a flock


def lock(name, wait=True):
    """Returns a FileLock, or None if the lock is busy and wait=False."""
    STATE.mkdir(parents=True, exist_ok=True)
    f = open(STATE / name, "a+")
    try:
        if os.name == "nt":
            import msvcrt   # LK_LOCK retries for about 10 s, then raises
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return FileLock(f)
    except OSError:
        f.close()
        return None


def start_speaker():
    """A speaker process that outlives the hook: its own session on macOS and Linux, detached on Windows."""
    argv = [sys.executable, os.path.abspath(__file__), "--speak-queue"]
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name != "nt":
        subprocess.Popen(argv, start_new_session=True, **quiet)
        return
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:   # leave Claude Code's job object, if it lets us, so closing the hook doesn't take the speaker along
        subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **quiet)
    except OSError:
        subprocess.Popen(argv, creationflags=flags, **quiet)


def enqueue(text, lang, spawn=True, who=""):
    """`who` is how the agent is called (or the repository): the title of the notification shown in a call."""
    item = {"text": text, "lang": lang}
    if who:
        item["who"] = who
    with lock("queue.lock"):
        with open(QUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    if spawn:
        start_speaker()


def requeue_front(item):
    """Puts an announcement back as the next one, whole: it was cut while being spoken and is said again."""
    with lock("queue.lock"):
        lines = QUEUE.read_text(encoding="utf-8").splitlines() if QUEUE.exists() else []
        QUEUE.write_text(json.dumps(item, ensure_ascii=False) + "\n" + "".join(line + "\n" for line in lines),
                         encoding="utf-8")


def waiting():
    return QUEUE.exists() and QUEUE.stat().st_size > 0


def dequeue():
    """The oldest announcement, or None when the queue is empty. A broken line is dropped."""
    while True:
        with lock("queue.lock"):
            lines = QUEUE.read_text(encoding="utf-8").splitlines() if QUEUE.exists() else []
            QUEUE.write_text("".join(line + "\n" for line in lines[1:]), encoding="utf-8")
        if not lines:
            return None
        try:
            item = json.loads(lines[0])
            if isinstance(item, dict) and item.get("text"):
                return item
        except ValueError:
            pass


def default_say(config):
    """The function that really speaks. Returns False if the microphone opened and cut it, else True."""
    def say(text, voice, lang, stop=None):
        try:
            speak(text, voice, lang, config.get("command") or "", stop)
        except Interrupted:
            return False
        return True
    return say


def notify_instead(item, lang, notifier):
    """In a call nothing is spoken: the announcement becomes a notification, and the log keeps it."""
    who = item.get("who") or "Claude"
    ok = notifier(PHRASES[lang]["call_title"].format(p=who), item["text"])
    write_log(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} call: {'notified' if ok else 'notification failed'} "
              f"{who}: {item['text']}")


def speak_queue(say=None, mic=None, notifier=None, sleep=time.sleep):
    """One speaker at a time. If something arrived between emptying the queue and releasing the lock, go again.

    Before each announcement it looks at the microphone (`mic()` returns "free", "busy" or "call"):
      free  speak it, and keep watching: if the microphone opens in the middle, the speech stops and the whole
            announcement goes back to the front of the queue;
      busy  an app is listening to you: wait for it to close (it stays in the queue), then speak;
      call  you are in a call: show a notification with the text and do not speak it."""
    config, voices = load_config(), {}
    if mic is None and mic_enabled(config):
        mic = mic_state
    notifier = notifier or notify
    say = say or default_say(config)
    while True:
        held = lock("speaker.lock", wait=False)
        if not held:
            return   # another speaker is running and will read this announcement too
        with held:
            waited = False   # true after a wait: the microphone must stay closed MIC_SETTLE before we speak
            while True:
                state = mic() if mic else "free"
                if state == "busy":
                    if not waiting():
                        break   # nothing to say: don't hang around
                    waited = True
                    sleep(MIC_POLL)
                    continue
                if state == "free" and waited:
                    waited = False
                    sleep(MIC_SETTLE)
                    continue   # look again: it may have opened during the pause
                item = dequeue()
                if not item:
                    break
                lang = item.get("lang") if item.get("lang") in LANGUAGES else language(config)
                if state == "call":
                    notify_instead(item, lang, notifier)
                    continue
                if lang not in voices:
                    voices[lang] = chosen_voice(lang, config)
                stop = (lambda: mic() != "free") if mic else None
                if say(item["text"], voices[lang], lang, stop=stop) is False:
                    requeue_front(item)   # cut: it is said again, whole, when the microphone is free
                    waited = True
        if not waiting():
            return


# --- what to say ---------------------------------------------------------------------------------------------

SUMMARY_MARK = re.compile(r"(?m)^[ \t>*_-]*🔊️?[ \t:]*")


def spoken_summary(text, limit=420):
    """From the last line that STARTS with 🔊 to the end of the reply, without markdown or links. "" if none."""
    marks = list(SUMMARY_MARK.finditer(text or ""))
    if not marks:
        return ""
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text[marks[-1].end():])   # [label](url) -> label
    t = " ".join(re.sub(r"https?://\S+|[`*_#>|]", " ", t).split())
    t = re.sub(r" ([,.;:!?])", r"\1", t)
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "."


def spoken_name(folder):
    """The project as you would say it: the repository (a worktree counts as its main repo), else the folder."""
    path = Path(folder)
    if path.is_dir():
        common = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(path)).stdout.strip()
        if common.endswith(".git"):
            path = Path(common).parent
    return " ".join(re.sub(r"[-_.]+", " ", path.name).split()) or "Claude"


def desktop_session_folder():
    """Folder the session started in, from the Claude desktop app's own session record (macOS).

    This file is internal to the app and has no contract: if it moves or changes, the other sources take over.
    """
    sid = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID")
    base = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    for f in (base.glob(f"*/*/{sid}.json") if sid and re.fullmatch(r"[\w-]+", sid) else []):
        try:
            return json.loads(f.read_text(encoding="utf-8")).get("originCwd") or ""
        except (ValueError, OSError, AttributeError):
            pass
    return ""


def session_folder(payload):
    """Where the session started. A `cd` in a Bash call moves the hook's `cwd`, so stable sources come first."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or desktop_session_folder() or payload.get("cwd") or os.getcwd()


NAME = r"[A-ZÀ-ÖØ-Ý][\w'’-]*(?:\s+[A-ZÀ-ÖØ-Ý][\w'’-]*){0,2}"   # up to three capitalized words
AGENT_INTRO = (   # "aqui é o Orion." / "aqui é a Luna," / "this is Orion." in the opening of the line
    re.compile(r"(?i:\baqui\s+é\s+(?P<art>o|a)\s+)(?P<name>" + NAME + r")(?=\s*[.,:;!]|\s*$)"),
    re.compile(r"(?i:\bthis\s+is\s+)(?P<name>" + NAME + r")(?=\s*[.,:;!]|\s*$)"),
)


def agent_from_summary(summary):
    """The (article, name) an agent gave in the opening of its 🔊 line ("aqui é o Orion", "this is Luna"),
    or None. The article ("o", "a" or "") is kept for Portuguese, to say "O Orion precisa de você"."""
    head = (summary or "")[:140]
    for pattern in AGENT_INTRO:
        m = pattern.search(head)
        if m:
            return (m.groupdict().get("art") or "").lower(), " ".join(m.group("name").split())
    return None


def agent_label(agent):
    """How to say the agent as the subject of a sentence: "O Orion", "A Luna", "Orion". "" without one."""
    if not agent:
        return ""
    article, name = agent
    label = f"{article} {name}".strip()
    return label[:1].upper() + label[1:]


def read_agents():
    try:
        data = json.loads(AGENTS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_agent(session_id, agent):
    """Keeps the name an agent used in this session, for the events it doesn't write (permission, question)."""
    if not session_id or not agent:
        return
    with lock("agents.lock"):
        data = read_agents()
        data[str(session_id)] = {"article": agent[0], "name": agent[1], "at": time.time()}
        newest = sorted(data.items(), key=lambda kv: kv[1].get("at", 0) if isinstance(kv[1], dict) else 0)
        AGENTS.write_text(json.dumps(dict(newest[-AGENTS_KEEP:]), ensure_ascii=False) + "\n", encoding="utf-8")


def recall_agent(session_id):
    """The (article, name) remembered for this session, or None."""
    entry = read_agents().get(str(session_id)) if session_id else None
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry.get("article") or ""), str(entry["name"])
    return None


def announcement(event, payload, project, lang, rng=random, agent=None):
    """The sentence to speak for a hook event, or None to stay quiet. `agent` is the (article, name) the agent
    gave earlier in this session: it replaces the repository name when the agent didn't write the sentence."""
    p = PHRASES[lang]
    who = agent_label(agent)
    if event == "Stop":
        summary = spoken_summary(payload.get("last_assistant_message"))
        if summary:
            return summary   # the agent speaks for itself: no "<repo> is done." in front
        if who:
            return rng.choice(p["done_named"]).format(p=who)
        return rng.choice(p["done"]).format(p=project)
    if event == "Notification":
        kind = payload.get("notification_type") or payload.get("message") or ""
        if "permission" in kind.lower():
            return rng.choice(p["approval"]).format(p=who or project)
        if re.search(r"elicitation|question", kind, re.I):
            return rng.choice(p["question"]).format(p=who or project)
    return None   # idle reminders and anything new stay quiet; the log shows them


def write_log(line):
    with lock("queue.lock"):   # two hooks at once must not drop each other's line
        lines = LOG.read_text(encoding="utf-8").splitlines()[-(LOG_LINES - 1):] if LOG.exists() else []
        LOG.write_text("\n".join(lines + [line]) + "\n", encoding="utf-8")


def hook(stdin_text):
    """Hook entry point. Returns what to print (only UserPromptSubmit prints). Never raises: a failing hook
    shows up in the session, and a notification is not worth that."""
    try:
        payload = json.loads(stdin_text or "{}")
        if not ENABLED.exists() or payload.get("stop_hook_active") \
                or os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):   # claude -p and the SDK stay quiet
            return ""
        config = load_config()
        if not can_speak(config):   # no engine and no program: no instruction either, so no tokens are spent
            return ""
        lang = language(config)
        event = payload.get("hook_event_name", "")
        if event == "UserPromptSubmit":
            # ASCII-only JSON: the 🔊 travels as an escape, safe on any console encoding
            return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": INSTRUCTION[lang]}})
        project = spoken_name(session_folder(payload))
        session_id = payload.get("session_id")
        agent = None
        if event == "Stop":   # the agent introduces itself in its own line: remember who it is in this session
            agent = agent_from_summary(spoken_summary(payload.get("last_assistant_message")))
            remember_agent(session_id, agent)
        agent = agent or recall_agent(session_id)
        text = announcement(event, payload, project, lang, agent=agent)
        kind = payload.get("notification_type") or ""
        write_log(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {event} [{kind}] {agent_label(agent) or project}: "
                  f"{text or '(quiet)'}")
        if text:
            enqueue(text, lang, who=agent_label(agent) or project)
    except Exception:
        pass
    return ""


# --- /voice-notify -------------------------------------------------------------------------------------------

NO_ENGINE = {
    "linux": "No speech engine found. Install speech-dispatcher (spd-say) or espeak-ng, or set your own program "
             "with /voice-notify command <program>.",
    "windows": "Windows PowerShell (powershell.exe) was not found. Set your own program with "
               "/voice-notify command <program>.",
}


def engine_line(config):
    if config.get("command"):
        return f"speaks with: your program `{config['command']}` (falls back to {system_engine() or 'nothing'})"
    return f"speaks with: {system_engine() or 'nothing'}"


def status():
    config = load_config()
    lang = language(config)
    out = [f"voice notifications: {'ON' if ENABLED.exists() else 'off'}",
           f"language: {lang}" + ("" if config.get("language") in LANGUAGES else " (auto, from the system)"),
           f"voice: {config.get('voice') or (chosen_voice(lang, config) or 'engine default') + ' (auto)'}",
           engine_line(config)]
    if not can_speak(config):
        out.append(NO_ENGINE.get(system(), "Nothing can speak here. Set a program with /voice-notify command <program>."))
    out.append(mic_line(config))
    if QUEUE.exists() and QUEUE.stat().st_size:
        out.append(f"queued: {len(QUEUE.read_text(encoding='utf-8').splitlines())}")
    if LOG.exists():
        out.append("last events:\n  " + "\n  ".join(LOG.read_text(encoding="utf-8").splitlines()[-5:]))
    return "\n".join(out)


def mic_line(config):
    if system() != "mac":
        return "microphone: not watched on this system (speaks as usual)"
    if config.get("mic_aware") is False:
        return "microphone: not watched (/voice-notify mic on)"
    report = mic_report(config)
    if report is None:
        return "microphone: can't be read here, it needs macOS 14 or newer (speaks as usual)"
    return f"microphone: watched, {report[0]} now (/voice-notify mic)"


def mic_valid(identifier):
    return bool(re.fullmatch(r"[\w.\-]{1,200}", identifier))


MIC_USAGE = ("usage: /voice-notify mic [on | off | now | ignore [add <id> | remove <id> | reset] | "
             "call [add <id> | remove <id> | reset]]")


def mic_command(rest):
    """/voice-notify mic ...: look at the microphone, switch the watching on or off, edit the two lists."""
    config = load_config()
    action = rest[0].lower() if rest else "status"
    if action in ("on", "off") and len(rest) == 1:
        save_config(mic_aware=action == "on")
        return mic_command([])
    if action == "now" and len(rest) == 1:
        report = mic_report(config)
        if report is None:
            return "The microphone can't be read here (it needs macOS 14 or newer)."
        meaning = {"ignored": "ignored", "call": "in a call: notifies, never speaks", "busy": "listening: it waits"}
        rows = [f"  {ident}  (pid {pid})  {meaning[role]}" for pid, ident, role in report[1]]
        return "\n".join([f"apps with the microphone input open now: {len(report[1])}"] + rows + [
            f"state: {report[0]}", "An app that should not count: /voice-notify mic ignore add <id>."])
    if action in ("ignore", "call"):
        key, default = ("mic_ignore", MIC_IGNORE) if action == "ignore" else ("mic_calls", MIC_CALLS)
        current, sub, arg = mic_list(config, key, default), (rest[1].lower() if len(rest) > 1 else "list"), rest[2:]
        if sub == "list" and not arg:
            pass
        elif sub == "add" and len(arg) == 1 and mic_valid(arg[0]):
            if not any(c.lower() == arg[0].lower() for c in current):
                save_config(**{key: current + [arg[0]]})
        elif sub == "remove" and len(arg) == 1 and mic_valid(arg[0]):
            save_config(**{key: [c for c in current if c.lower() != arg[0].lower()]})
        elif sub == "reset" and not arg:
            save_config(**{key: None})
        else:
            return MIC_USAGE
        return mic_command([])
    if action == "status" and len(rest) <= 1:
        calls, ignored = mic_list(config, "mic_calls", MIC_CALLS), mic_list(config, "mic_ignore", MIC_IGNORE)
        return "\n".join([
            mic_line(config),
            "  an app listening to you: it waits and speaks when the input closes; cut in the middle, it starts over",
            f"  call apps (notification, no speech): {', '.join(calls) or '(none)'}",
            f"  ignored (always listening): {', '.join(ignored) or '(none)'}",
            "  edit: /voice-notify mic call|ignore add|remove|reset <id>; who is listening now: /voice-notify mic now"])
    return MIC_USAGE


def list_voices():
    config = load_config()
    lang = language(config)
    engine = system_engine()
    if engine in ("spd-say", "espeak-ng"):
        listing = run(["spd-say", "-L"] if engine == "spd-say" else ["espeak-ng", f"--voices={lang.split('-')[0]}"])
        return "\n".join([f"{engine} voices (the engine picks one for {lang} unless you choose):",
                          listing.stdout.rstrip() or "  (none listed)", "",
                          "Pick one with: /voice-notify voice <name>   (or /voice-notify voice auto)"])
    voices = sorted((v for v in installed_voices() if v[2].lower().startswith(lang.lower())),
                    key=lambda v: voice_rank(v, lang), reverse=True)
    current = chosen_voice(lang, config)
    rows = [f"{'*' if v[0] == current else ' '} {v[0]}  ({v[1]}, {v[2]}, {QUALITY.get(v[3], v[3])})" for v in voices]
    more = {"avspeech": "More voices: System Settings > Accessibility > Spoken Content > System voice > Manage Voices.",
            "sapi": "More voices: Settings > Time & language > Speech > Manage voices > Add voices."}.get(engine, "")
    return "\n".join([f"voices for {lang} (* = in use):"] + (rows or ["  none found"]) + [
        "", more, "Pick one with: /voice-notify voice <identifier>   (or /voice-notify voice auto)"]).replace("\n\n\n", "\n\n")


def join_args(args):
    """Rebuilds a command line from the words the shell split, quoting for the shell that will run it."""
    return subprocess.list2cmdline(args) if system() == "windows" else " ".join(shlex.quote(a) for a in args)


USAGE = ("usage: /voice-notify [on [voice] | off | test | voices | status | lang <en|pt-BR|auto> | "
         "voice <name|auto> | command <program...|off> | mic [on|off|now|ignore ...|call ...]]")


def command(args):
    """The /voice-notify command. Returns the text the command shows."""
    action, rest = (args[0].lower(), args[1:]) if args else ("status", [])
    if action == "on":
        STATE.mkdir(parents=True, exist_ok=True)
        if rest:
            save_config(voice=" ".join(rest))
        ENABLED.write_text("", encoding="utf-8")
        if not can_speak():
            return status()
        lang = language()
        enqueue(PHRASES[lang]["on"], lang)
        return status() + "\n\nApplies from your next message, in every open session."
    if action == "off":
        ENABLED.unlink(missing_ok=True)
        return status()
    if action == "test":
        if not can_speak():
            return status()
        lang = language()
        enqueue(PHRASES[lang]["test"], lang)
        return "Speaking a test announcement" + ("." if ENABLED.exists() else " (notifications are off: /voice-notify on).")
    if action == "voices":
        return list_voices()
    if action == "lang" and rest and (rest[0] in LANGUAGES or rest[0] == "auto"):
        save_config(language=rest[0])
        return status()
    if action == "voice" and rest:
        save_config(voice="" if rest == ["auto"] else " ".join(rest))
        return status()
    if action == "command" and rest:
        save_config(command="" if rest == ["off"] else join_args(rest))
        return status()
    if action == "mic":
        return mic_command(rest)
    if action == "status":
        return status()
    return USAGE


def main(argv):
    if argv[:1] == ["hook"]:
        # Read bytes: the hook input is UTF-8 whatever the console encoding (Windows uses a code page)
        out = hook(sys.stdin.buffer.read().decode("utf-8", "replace"))
        if out:
            print(out)
    elif argv[:1] == ["--speak-queue"]:
        speak_queue()
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        print(command(argv))


if __name__ == "__main__":
    main(sys.argv[1:])
