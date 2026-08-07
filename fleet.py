#!/usr/bin/env python3
"""Agent Town - local dashboard for Claude Code subagents.

Each desk has its own git worktree, so several agents can write at once
without stepping on each other. Run:  python3 fleet.py  ->  http://127.0.0.1:8765
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.environ.get("FLEET_PROJECT", os.path.expanduser("~/odoo/custom_addons"))
BASE = os.environ.get("FLEET_BASE", "main")
REPORTS = os.path.join(HERE, "reports")
WT = os.path.join(HERE, "wt")
UPLOADS = os.path.join(HERE, "uploads")
PORT = int(os.environ.get("FLEET_PORT", "8765"))

ODOO_VERSION = os.environ.get("FLEET_ODOO_VERSION", "17")

# Optional: name of a custom subagent (see `claude agent` / .claude/agents/*.md)
# to use for reviews. Unset by default so this works out of the box on a stock
# Claude Code install - set FLEET_REVIEW_AGENT if you have a dedicated one.
REVIEW_AGENT = os.environ.get("FLEET_REVIEW_AGENT") or None

# Odoo Enterprise-specific conventions - attached to every write-type prompt so
# the agent uses version-appropriate patterns (the repo's own CLAUDE.md Zero
# Trust policy is separate; this is just an extra Odoo-syntax layer on top).
ODOO_STANDARDS = (
    "Follow Odoo {v} Enterprise conventions: manifest 'version' key in "
    "'{v}.0.x.x.x' format, security/ir.model.access.csv and security groups "
    "for every new model, <odoo> as the XML views root (not the legacy "
    "<openerp>), OWL 17 components (not legacy widgets). Strictly follow the "
    "repo's CLAUDE.md Zero Trust policy if one exists (no sudo(), no "
    "cr.execute(), no base.group_user, no hardcoded user/group assignments)."
).format(v=ODOO_VERSION)

DESKS = ["desk-1", "desk-2", "desk-3", "desk-4", "desk-5", "desk-6", "urgent-1", "urgent-2"]
# urgent-1/2 have their own "room" - always kept empty so real urgent work
# never has to queue behind normal work. Fast model, AI triage skipped too.
URGENT = {"urgent-1", "urgent-2"}
DEFAULT_NAMES = {
    "desk-1": "Byte", "desk-2": "Cortex", "desk-3": "Vector",
    "desk-4": "Nova", "desk-5": "Quark", "desk-6": "Flux",
    "urgent-1": "Blitz", "urgent-2": "Siren",
}
NAMES_FILE = os.path.join(HERE, "names.json")


def load_names():
    try:
        with open(NAMES_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    names = dict(DEFAULT_NAMES)
    names.update({k: v for k, v in saved.items() if k in DEFAULT_NAMES and v.strip()})
    return names


def save_names():
    try:
        with open(NAMES_FILE, "w") as f:
            json.dump(NAMES, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


# Can be renamed from the UI, so NAMES is mutable (persisted to a JSON file).
# blank() and /rename both read/write this same dict.
NAMES = load_names()
MODEL_IDS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
}
# One fixed shipper. It only reads and gives an opinion - push is done by
# Python; no agent has a commit/push tool.
SHIPPER = "shipper"

# Read tools every task needs.
READ_TOOLS = ["Read", "Grep", "Glob",
              "Bash(git diff:*)", "Bash(git log:*)", "Bash(git status:*)", "Bash(git show:*)"]
# Write desks also get Edit/Write. Write creates parent dirs itself, so no
# mkdir/shell is needed - Bash stays read-only.
WRITE_TOOLS = READ_TOOLS + ["Edit", "Write"]

# Each task type: which agent, which tools, and the prompt template. A
# headless agent can't ask questions, so the template front-loads the details
# it needs.
TASK_TYPES = {
    "module": {
        "label": "New module",
        "agent": None,
        "tools": WRITE_TOOLS,
        "template": (
            "Create a new Odoo {v} custom addon: {{task}}\n\n"
            "Write the whole module: __init__.py, __manifest__.py, models/, "
            "security/ir.model.access.csv and security groups, views/.\n"
            "Look at the style of the existing addons and follow the same "
            "conventions. At the end, report which files you created and "
            "what's left.").format(v=ODOO_VERSION),
    },
    "feature": {
        "label": "New feature",
        "agent": None,
        "tools": WRITE_TOOLS,
        "template": (
            "Add a new feature to an existing module: {task}\n\n"
            "First read the related files to understand the existing "
            "pattern, then write in the same style. Keep the diff as small "
            "as you can. At the end, list every file you changed and why."),
    },
    "change": {
        "label": "Change / Update",
        "agent": None,
        "tools": WRITE_TOOLS,
        "template": (
            "Make this change to the existing code: {task}\n\n"
            "Only change what was asked - no unrelated refactoring. At the "
            "end, list every file you changed and why."),
    },
    "fix": {
        "label": "Bug fix",
        "agent": None,
        "tools": WRITE_TOOLS,
        "template": (
            "Fix this bug: {task}\n\n"
            "Find the root cause first - don't just patch the symptom. "
            "Check the other callers of any function you change. At the "
            "end, explain what the root cause was and where you fixed it."),
    },
    "review": {
        "label": "Review (read-only)",
        "agent": REVIEW_AGENT,
        "tools": READ_TOOLS,
        "template": (
            "Review this: {task}\n\n"
            "Follow the review checklist in CLAUDE.md. Give file:line for "
            "every finding. Don't write anything, just report."),
    },
}


# Keeps the system from sleeping while an agent is running. Suspend stops the
# CPU entirely - no software can work around that, so preventing it is the
# only fix.
INHIBIT = ["systemd-inhibit", "--what=sleep:idle", "--who=Agent Town",
           "--why=an agent is running", "--mode=block", "--"]


def blank(name):
    return {"agent": name, "name": NAMES.get(name, name), "urgent": name in URGENT,
            "status": "empty", "tool": "", "detail": "", "task": "", "ttype": "",
            "domain": "", "model": "", "ai_urgent": False,
            "turns": 0, "cost": 0.0, "report": None, "started": 0.0,
            "log": [], "output": "", "branch": "fleet/%s" % name, "changed": 0}


STATE = {n: blank(n) for n in DESKS}
STATE[SHIPPER] = blank(SHIPPER)
STATE[SHIPPER]["reviewed"] = ""   # which desk was reviewed
STATE[SHIPPER]["verdict"] = ""    # the review's verdict text
LOCK = threading.Lock()
# Running subprocesses are tracked here so /stop can terminate them.
PROC = {}
PROC_LOCK = threading.Lock()
STOPPING = set()


def set_(desk, **kw):
    """desk is a positional param (not `name`) on purpose: STATE entries have
    their own "name" field (see blank()), and callers regularly do
    set_(desk, **blank(desk)) or **patch dicts that include "name" - if this
    parameter were itself called `name`, that would collide and crash with
    "multiple values for argument 'name'"."""
    with LOCK:
        STATE[desk].update(kw)


def rename_agent(desk, new_name):
    """Set a new name from the UI - both NAMES (persisted) and the live
    STATE get updated. Returns (ok, name_or_error). Checked by selftest."""
    new_name = (new_name or "").strip()[:24]
    if desk not in STATE:
        return False, "unknown desk"
    if not new_name:
        return False, "name cannot be empty"
    NAMES[desk] = new_name
    save_names()
    with LOCK:
        STATE[desk]["name"] = new_name
    return True, new_name


def git(args, cwd=PROJECT):
    """Run git, return (rc, stdout). Never raises, even if cwd doesn't exist."""
    try:
        p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    except OSError as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


RESUME_PROMPT = (
    "There is already work in progress in this worktree. Run `git status` and "
    "`git diff` to see what's been written so far, then add this new "
    "requirement/change to the same work:\n\n{task}\n\n"
    "Don't touch what's already correct, only add or adjust what's new. The "
    "repo's CLAUDE.md Zero Trust policy still applies. At the end, explain "
    "what you changed."
)

# When creating a new module, "which module" is already implicit (its name is
# in the task text); every other type expects an existing module.
MODULE_OPTIONAL_TYPES = {"module"}


def attachment_note(attachments):
    """Reference text for uploaded files - the Read tool can read
    images/PDF/Excel itself, we just need to point at the path."""
    if not attachments:
        return ""
    lines = "\n".join("- %s" % a.get("path", a) for a in attachments)
    return ("\n\nThese files were also provided - read them with the Read "
            "tool and use them in the work:\n%s" % lines)


def build_prompt(ttype, task, module="", domain="", attachments=None):
    """Task type + user's text + (if given) module/domain/attachments ->
    the full prompt. Checked by selftest."""
    spec = TASK_TYPES.get(ttype)
    if not spec:
        raise KeyError(ttype)
    body = spec["template"].format(task=task.strip()) + attachment_note(attachments)
    if module and ttype not in MODULE_OPTIONAL_TYPES:
        body = ("Module: `%s` (custom_addons/%s/) - work inside this module "
                 "only, don't touch anything outside it.\n\n" % (module, module)) + body
    if domain:
        body = ("Domain: %s\n\n" % domain.strip()) + body
    return body + "\n\n" + ODOO_STANDARDS


def desk_path(name):
    return os.path.join(WT, name)


def desk_dirty(name):
    """Is there unreviewed work sitting in the worktree?"""
    path = desk_path(name)
    if not os.path.isdir(path):
        return False
    rc, out = git(["status", "--porcelain"], cwd=path)
    return rc == 0 and bool(out.strip())


# The UI polls every 800ms; running git status every time is wasteful since
# the count only changes when the agent writes something. A 3s cache is fine.
_CHANGED_TTL = 3.0
_changed_cache = {}


def desk_changed_files(name, fresh=False):
    now = time.time()
    hit = _changed_cache.get(name)
    if not fresh and hit and now - hit[0] < _CHANGED_TTL:
        return hit[1]
    path = desk_path(name)
    if not os.path.isdir(path):
        n = 0
    else:
        rc, out = git(["status", "--porcelain"], cwd=path)
        n = len([l for l in out.splitlines() if l.strip()]) if rc == 0 else 0
    _changed_cache[name] = (now, n)
    return n


def ensure_worktree(name):
    """Prepare the desk's worktree, reset to BASE. Returns (ok, message)."""
    path = desk_path(name)
    branch = "fleet/%s" % name
    os.makedirs(WT, exist_ok=True)
    if not os.path.isdir(os.path.join(path, ".git")) and not os.path.exists(path):
        # If the wt/ dir was ever deleted by hand (rm -rf) instead of via
        # `git worktree remove`, PROJECT's .git still thinks this branch's
        # worktree exists at the old path and refuses to reuse it. Prune
        # stale registrations first so this self-heals instead of getting
        # permanently stuck on "already used by worktree".
        git(["worktree", "prune"])
        rc, out = git(["worktree", "add", "-B", branch, path, BASE])
        if rc != 0:
            return False, out.strip().splitlines()[-1][:160] if out.strip() else "worktree add failed"
        return True, "worktree created"
    # Existing worktree - if it's clean, bring it back to BASE (all local, no network)
    rc, out = git(["reset", "--hard", BASE], cwd=path)
    if rc != 0:
        return False, out.strip().splitlines()[-1][:160] if out.strip() else "reset failed"
    git(["clean", "-fd"], cwd=path)
    return True, "worktree reset"


def handle_event(ev):
    """stream-json event -> desk state patch (or None). Checked by selftest()."""
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        return {"status": "seated", "tool": "", "detail": "booting"}
    if t == "assistant":
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") == "thinking":
                return {"status": "thinking", "tool": "", "detail": "reasoning"}
            if b.get("type") == "tool_use":
                inp = b.get("input") or {}
                d = (inp.get("file_path") or inp.get("pattern") or inp.get("command")
                     or inp.get("path") or inp.get("query") or "")
                return {"status": "working", "tool": b.get("name", "?"), "detail": str(d)[:70]}
        return {"status": "thinking", "tool": "", "detail": "writing"}
    if t == "result":
        ok = ev.get("subtype") == "success"
        return {"status": "done" if ok else "error",
                "tool": "",
                "detail": "finished" if ok else str(ev.get("subtype") or "failed"),
                "turns": ev.get("num_turns") or 0,
                "cost": ev.get("total_cost_usd") or 0.0}
    return None


def run_agent(name, ttype, task, module="", domain="", attachments=None, resume=False):
    """resume=True: the worktree is NOT reset - previous work is preserved,
    the new requirement is added on top of it (in case the user missed
    something).

    Model and urgency are decided by AI itself (triage_task) - only the
    Urgent Room and resume calls skip this (need to stay fast / model is
    already fixed)."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    urgent = name in URGENT
    if resume:
        prompt = RESUME_PROMPT.format(task=task.strip()) + attachment_note(attachments)
        tools, agent_flag = WRITE_TOOLS, None
        display_task = (STATE[name]["task"] or "") + "\n+ " + task.strip()
        model_key, ai_urgent = STATE[name].get("model") or "sonnet", STATE[name].get("ai_urgent", False)
    else:
        spec = TASK_TYPES[ttype]
        prompt = build_prompt(ttype, task, module, domain, attachments)
        tools, agent_flag = spec["tools"], spec["agent"]
        display_task = task
        if urgent:
            model_key, ai_urgent = "sonnet", True
        else:
            set_(name, status="seated", task=display_task, ttype=ttype, domain=domain,
                 tool="", detail="deciding on AI model and urgency…",
                 turns=0, cost=0.0, report=None, started=time.time(), log=[],
                 output="", changed=0)
            model_key, ai_urgent = triage_task(ttype, task, domain)

    set_(name, status="seated", task=display_task, ttype=ttype, domain=domain,
         model=model_key, urgent=urgent, ai_urgent=ai_urgent,
         tool="", detail="preparing worktree", turns=0, cost=0.0, report=None,
         started=time.time(), log=[], output="", changed=0)

    if not resume:
        ok, msg = ensure_worktree(name)
        if not ok:
            set_(name, status="error", detail=msg)
            return

    cmd = ["claude", "-p", prompt, "--model", MODEL_IDS.get(model_key, MODEL_IDS["sonnet"]),
           "--output-format", "stream-json", "--verbose", "--allowedTools", *tools]
    if agent_flag:
        cmd = cmd[:5] + ["--agent", agent_flag] + cmd[5:]
    if attachments:
        cmd += ["--add-dir", UPLOADS]  # uploads live outside the worktree, path must be given explicitly
    if shutil.which("systemd-inhibit"):
        cmd = INHIBIT + cmd
    try:
        p = subprocess.Popen(cmd, cwd=desk_path(name), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        set_(name, status="error", detail="claude CLI not found: %s" % e)
        return
    with PROC_LOCK:
        PROC[name] = p

    final = ""
    for line in p.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            final = ev.get("result") or ""
        patch = handle_event(ev)
        if patch:
            set_(name, **patch)
            if patch.get("tool"):
                with LOCK:
                    log = STATE[name]["log"]
                    log.append({"t": time.time(), "tool": patch["tool"],
                                "detail": patch["detail"]})
                    del log[:-60]

    err = (p.stderr.read() or "").strip()
    p.wait()
    with PROC_LOCK:
        PROC.pop(name, None)
    was_stopped = name in STOPPING
    STOPPING.discard(name)
    set_(name, changed=desk_changed_files(name, fresh=True))

    if was_stopped:
        set_(name, status="stopped", tool="",
             detail="stopped by user - work done so far is preserved in the worktree")
        return

    if p.returncode != 0 and not final:
        tail = err.strip() or "exit %d" % p.returncode
        set_(name, status="error", detail=tail.splitlines()[-1][:120], output=tail[-4000:])
        return

    os.makedirs(REPORTS, exist_ok=True)
    fn = "%s-%s-%s.md" % (name, ttype, ts)
    with open(os.path.join(REPORTS, fn), "w") as f:
        f.write("# %s / %s\n\n**Task:** %s\n\n**Worktree:** %s\n\n---\n\n%s\n"
                % (name, ttype, display_task, desk_path(name), final or "(no output)"))
    set_(name, report=fn, output=final or "(no output)")


# Branch list comes from git, not hardcoded. Cached because the UI polls
# frequently.
_BRANCH_TTL = 30.0
_branch_cache = [0.0, []]


def list_branches():
    now = time.time()
    if now - _branch_cache[0] < _BRANCH_TTL and _branch_cache[1]:
        return _branch_cache[1]
    rc, out = git(["for-each-ref", "--sort=-committerdate", "--format=%(refname:lstrip=3)",
                   "refs/remotes/origin"])
    names = []
    for line in out.splitlines():
        b = line.strip()
        if not b or b == "HEAD" or b in ("main", "master") or b in names:
            continue
        names.append(b)
    _branch_cache[0], _branch_cache[1] = now, names
    return names


_MODULE_TTL = 30.0
_module_cache = [0.0, []]


# Data uploaded and handed to the agent - the Read tool can read
# images/PDF/Excel itself, so there's no parsing to do here, just save the
# file safely and give the agent the path via --add-dir.
UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
               ".xlsx", ".xls", ".csv", ".docx", ".txt"}
UPLOAD_MAX_BYTES = 20 * 1024 * 1024  # 20MB decoded


def save_upload(filename, data_b64):
    """Returns (ok, path_or_error). Filename is sanitized (path traversal),
    extension is checked against an allowlist, size cap is enforced."""
    safe_name = os.path.basename((filename or "upload").strip()) or "upload"
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in UPLOAD_EXTS:
        return False, "%s file type is not supported (%s allowed)" % (ext or "(no ext)",
                                                                    ", ".join(sorted(UPLOAD_EXTS)))
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (ValueError, TypeError):
        return False, "file data is invalid/corrupt"
    if len(raw) > UPLOAD_MAX_BYTES:
        return False, "file is %dMB, over the %dMB max" % (
            len(raw) // (1024 * 1024), UPLOAD_MAX_BYTES // (1024 * 1024))
    os.makedirs(UPLOADS, exist_ok=True)
    stored = "%s-%s" % (uuid.uuid4().hex[:8], safe_name)
    path = os.path.join(UPLOADS, stored)
    with open(path, "wb") as f:
        f.write(raw)
    return True, path


# ---------- voice input (whisper.cpp, fully local - no API key) ----------
WHISPER_BIN = os.path.join(HERE, "whisper.cpp", "build", "bin", "whisper-cli")
WHISPER_MODEL = os.path.join(HERE, "whisper.cpp", "models", "ggml-base.bin")
ARABIC_SCRIPT_RE = re.compile(r"[؀-ۿݐ-ݿ]")
TRANSLATE_MODEL = "claude-haiku-4-5-20251001"  # lightweight - just a short translation


def voice_ready():
    return os.path.isfile(WHISPER_BIN) and os.path.isfile(WHISPER_MODEL)


def transcribe_audio(audio_path):
    """Recording from the browser (webm/ogg) -> text. ffmpeg makes a 16kHz
    mono WAV (what whisper.cpp needs), then whisper-cli runs on it. Both temp
    files are always cleaned up, whether it succeeds or fails."""
    if not voice_ready():
        return False, "voice input is not set up yet (whisper.cpp missing)"
    wav_path = audio_path + ".wav"
    try:
        conv = subprocess.run(["ffmpeg", "-y", "-i", audio_path, "-ar", "16000",
                                "-ac", "1", "-f", "wav", wav_path],
                               capture_output=True, text=True, timeout=30)
        if conv.returncode != 0:
            return False, "couldn't process the audio: " + last_line(conv.stderr, "ffmpeg error")
        try:
            r = subprocess.run([WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav_path,
                                 "-nt", "-np", "-l", "auto"],
                                capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            return False, "transcription timed out"
        text = (r.stdout or "").strip()
        if not text:
            return False, "couldn't make out anything - try again"
        return True, text
    finally:
        for pth in (audio_path, wav_path):
            try:
                os.remove(pth)
            except OSError:
                pass


TRANSLATE_PROMPT = (
    "This text is in Urdu/Arabic script:\n\n{text}\n\n"
    "Translate it into English, keeping the meaning intact. "
    "Return only the translation, nothing else."
)


def translate_to_english(text):
    """If the text is in Arabic/Urdu script, translate it into English - this
    tool is English-only, and voice input should match that. Fail-safe: if
    anything goes wrong, the original text is returned unchanged."""
    if not ARABIC_SCRIPT_RE.search(text):
        return text
    prompt = TRANSLATE_PROMPT.format(text=text)
    cmd = ["claude", "-p", prompt, "--model", TRANSLATE_MODEL,
           "--output-format", "stream-json", "--verbose", "--allowedTools", *READ_TOOLS]
    try:
        r = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return text
    answer = ""
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            answer = (ev.get("result") or "").strip()
    return answer or text


def list_modules():
    """Existing Odoo addons - top-level dirs that have a __manifest__.py."""
    now = time.time()
    if now - _module_cache[0] < _MODULE_TTL and _module_cache[1]:
        return _module_cache[1]
    try:
        names = sorted(d for d in os.listdir(PROJECT)
                        if os.path.isfile(os.path.join(PROJECT, d, "__manifest__.py")))
    except OSError:
        names = []
    _module_cache[0], _module_cache[1] = now, names
    return names


# Classification only (a one-word answer), doesn't need deep reasoning - use a
# light/cheap model so this AI suggestion doesn't eat into the plan's rate
# limit.
SUGGEST_MODEL = "claude-haiku-4-5-20251001"
SUGGEST_PROMPT = (
    "Here is the list of existing Odoo addons:\n{modules}\n\n"
    "User's task: {task}\n\n"
    "If one of these modules is the best fit for this task, write just its "
    "name exactly as it appears in the list (no extra words). If none of "
    "them fit, just write NEW. Answer with only that one word, nothing else."
)


def suggest_module(task):
    """Asks AI which existing module is relevant to this task text. Returns
    (module_name, reason) - module is empty if nothing suitable was found."""
    mods = list_modules()
    if not mods:
        return "", "no existing modules found"
    prompt = SUGGEST_PROMPT.format(modules="\n".join("- " + m for m in mods), task=task.strip())
    cmd = ["claude", "-p", prompt, "--model", SUGGEST_MODEL,
           "--output-format", "stream-json", "--verbose", "--allowedTools", *READ_TOOLS]
    try:
        r = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", "AI call failed: %s" % e
    answer = ""
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            answer = (ev.get("result") or "").strip()
    guess = answer.splitlines()[0].strip() if answer else ""
    if guess in mods:
        return guess, "AI suggested this for the task"
    return "", "AI didn't think any existing module fit - choose 'new module'"

TRIAGE_MODEL = SUGGEST_MODEL  # cheap/fast - this is also just classification
TRIAGE_PROMPT = (
    "This is an Odoo {v} development task that will be given to a coding agent:\n\n"
    "Type: {ttype}\nDomain: {domain}\nTask: {task}\n\n"
    "Make two decisions:\n"
    "1) Which model fits this work best - haiku (small/mechanical/single-file "
    "work), sonnet (normal feature/bug/module work), or opus (hard, "
    "architecture-level, multi-module, or risk-sensitive work)?\n"
    "2) Does the task text itself signal that this is production-urgent/"
    "critical (e.g. 'urgent', 'production down', 'client is waiting', 'ASAP')?\n\n"
    "Answer with exactly these two lines, nothing else:\n"
    "MODEL: haiku|sonnet|opus\n"
    "URGENT: yes|no"
)


def triage_task(ttype, task, domain):
    """Asks AI which model fits and whether the task sounds urgent on its
    own. Fail-safe: if anything goes wrong, returns (sonnet, False) - never
    crashes, and the work proceeds on sonnet."""
    prompt = TRIAGE_PROMPT.format(v=ODOO_VERSION, ttype=ttype,
                                   domain=domain.strip() or "(not given)", task=task.strip())
    cmd = ["claude", "-p", prompt, "--model", TRIAGE_MODEL,
           "--output-format", "stream-json", "--verbose", "--allowedTools", *READ_TOOLS]
    try:
        r = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return "sonnet", False
    answer = ""
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            answer = ev.get("result") or ""
    model, urgent = "sonnet", False
    for line in answer.splitlines():
        line = line.strip().lower()
        if line.startswith("model:"):
            v = line.split(":", 1)[1].strip()
            if v in MODEL_IDS:
                model = v
        elif line.startswith("urgent:"):
            urgent = line.split(":", 1)[1].strip().startswith("y")
    return model, urgent



def free_desk(urgent=False):
    """urgent=True -> only look in the Urgent Room, otherwise normal desks.
    Keeping the room separate means urgent work never gets stuck behind
    normal work."""
    pool = [n for n in DESKS if (n in URGENT) == urgent]
    with LOCK:
        for n in pool:
            if STATE[n]["status"] in ("empty", "done", "error", "stopped"):
                if not desk_dirty(n):
                    return n
    return None


REVIEW_PROMPT = (
    "There is uncommitted work in this worktree. Review it.\n\n"
    "The original task that was given: {task}\n\n"
    "Run `git status` and `git diff` to see what changed. New (untracked) "
    "files won't show up in git diff - read those with Read.\n\n"
    "Pay close attention to the repo's CLAUDE.md Zero Trust policy: no "
    "sudo(), no cr.execute(), no base.group_user in access rights, no "
    "hardcoded user/group assignment in XML data.\n\n"
    "On the first line write only: VERDICT: PASS  or  VERDICT: FAIL\n"
    "Then list every issue with file:line. Don't change anything, just give "
    "your opinion."
)


def last_line(out, fallback):
    lines = [l for l in (out or "").strip().splitlines() if l.strip()]
    return lines[-1][:200] if lines else fallback


def review_desk(desk):
    """Read-only review of a desk's work by the shipper. Doesn't push anything."""
    task = STATE[desk]["task"] or "(task wasn't recorded)"
    set_(SHIPPER, status="seated", task="review: %s" % desk, ttype="review",
         tool="", detail="reading the diff", started=time.time(), log=[],
         output="", turns=0, cost=0.0, reviewed=desk, verdict="")
    cmd = ["claude", "-p", REVIEW_PROMPT.format(task=task),
           "--output-format", "stream-json", "--verbose",
           "--allowedTools", *READ_TOOLS]
    if REVIEW_AGENT:
        cmd = cmd[:3] + ["--agent", REVIEW_AGENT] + cmd[3:]
    if shutil.which("systemd-inhibit"):
        cmd = INHIBIT + cmd
    try:
        pr = subprocess.Popen(cmd, cwd=desk_path(desk), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as e:
        set_(SHIPPER, status="error", detail="claude CLI not found: %s" % e)
        return
    with PROC_LOCK:
        PROC[SHIPPER] = pr
    final = ""
    for line in pr.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            final = ev.get("result") or ""
        patch = handle_event(ev)
        if patch:
            set_(SHIPPER, **patch)
    err = (pr.stderr.read() or "").strip()
    pr.wait()
    with PROC_LOCK:
        PROC.pop(SHIPPER, None)
    was_stopped = SHIPPER in STOPPING
    STOPPING.discard(SHIPPER)
    if was_stopped:
        set_(SHIPPER, status="stopped", detail="review stopped")
        return
    if pr.returncode != 0 and not final:
        set_(SHIPPER, status="error", detail=last_line(err, "exit %d" % pr.returncode),
             output=err[-4000:])
        return
    set_(SHIPPER, output=final or "(no output)", verdict=final or "")


def ship_desk(desk, branches, message):
    """Push the worktree's work to every given branch. Python-only git - no
    agent is involved.

    Each branch gets its own cherry-pick, because different target branches
    can have different history - one commit won't fast-forward on both.
    No agent has a commit/push tool; this step only runs on the user's OK.
    """
    branches = [b.strip() for b in branches if b and b.strip()]
    if not branches:
        return False, "no branch selected"
    bad = [b for b in branches if b in ("main", "master")]
    if bad:
        return False, "can't push straight to main/master - use another branch"
    path = desk_path(desk)
    if not os.path.isdir(path):
        return False, "this desk's worktree doesn't exist"
    if not desk_dirty(desk):
        return False, "no changes on this desk"

    rc, out = git(["add", "-A"], cwd=path)
    if rc:
        return False, last_line(out, "git add failed")
    rc, out = git(["commit", "-m", message], cwd=path)
    if rc:
        return False, last_line(out, "git commit failed")
    rc, sha = git(["rev-parse", "HEAD"], cwd=path)
    sha = sha.strip()

    done, home = [], "fleet/%s" % desk
    for br in branches:
        rc, out = git(["fetch", "origin", br], cwd=path)
        if rc:
            git(["checkout", home], cwd=path)
            return False, _partial(done, "%s: fetch failed - does the branch exist?" % br)
        rc, out = git(["checkout", "--detach", "origin/%s" % br], cwd=path)
        if rc:
            git(["checkout", home], cwd=path)
            return False, _partial(done, "%s: checkout failed" % br)
        rc, out = git(["cherry-pick", sha], cwd=path)
        if rc:
            git(["cherry-pick", "--abort"], cwd=path)
            git(["checkout", home], cwd=path)
            return False, _partial(done, "%s: cherry-pick conflict - resolve it by hand (%s)" % (br, path))
        rc, out = git(["push", "origin", "HEAD:%s" % br], cwd=path)
        if rc:
            git(["checkout", home], cwd=path)
            return False, _partial(done, "%s: push failed - %s" % (br, last_line(out, "")))
        done.append(br)

    git(["checkout", home], cwd=path)
    return True, "%s -> %s pushed" % (desk, " + ".join("origin/" + b for b in done))


def _partial(done, why):
    """Don't hide it when only part of the work went through - the user
    needs to know."""
    if not done:
        return why
    return "%s (but already pushed to %s)" % (why, " + ".join(done))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        # Strip any query string (?debug=1 and the like, e.g. from browser
        # extensions) before matching routes, so an extra ?param doesn't 404.
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, b"index.html missing", "text/plain")
        if path == "/state":
            with LOCK:
                snap = json.loads(json.dumps(STATE))
            for n in DESKS:
                if snap[n]["status"] in ("empty", "done", "error", "stopped"):
                    snap[n]["changed"] = desk_changed_files(n)
            types = [{"id": k, "label": v["label"]} for k, v in TASK_TYPES.items()]
            return self._send(200, json.dumps({"desks": [snap[n] for n in DESKS],
                                               "urgent_desks": sorted(URGENT),
                                               "shipper": snap[SHIPPER],
                                               "types": types, "project": PROJECT,
                                               "base": BASE, "odoo_version": ODOO_VERSION,
                                               "voice_ready": voice_ready(),
                                               "now": time.time()}))
        if path == "/branches":
            names = list_branches()
            # pre-checked by default - the user usually wants both base and stage
            return self._send(200, json.dumps({"branches": names,
                                               "default": [b for b in (BASE, "stage") if b in names]}))
        if path == "/modules":
            return self._send(200, json.dumps({"modules": list_modules()}))
        if path.startswith("/diff/"):
            n = os.path.basename(path[len("/diff/"):])
            if n not in STATE:
                return self._send(404, b"unknown desk", "text/plain")
            rc, out = git(["diff", "--stat", "HEAD"], cwd=desk_path(n))
            rc2, out2 = git(["diff", "HEAD"], cwd=desk_path(n))
            rc3, untracked = git(["ls-files", "--others", "--exclude-standard"], cwd=desk_path(n))
            body = out + ("\n=== new files ===\n" + untracked if untracked.strip() else "") \
                   + "\n\n" + out2
            return self._send(200, body.encode("utf-8"), "text/plain; charset=utf-8")
        if path.startswith("/reports/"):
            fn = os.path.basename(path[len("/reports/"):])
            try:
                with open(os.path.join(REPORTS, fn), "rb") as f:
                    return self._send(200, f.read(), "text/plain; charset=utf-8")
            except OSError:
                return self._send(404, b"no report", "text/plain")
        return self._send(404, b"nope", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        if n > 28 * 1024 * 1024:  # base64 is ~33% bigger, margin for the 20MB file cap
            self.close_connection = True  # skip reading the rest of the body - otherwise
            return self._send(400, json.dumps({"error": "request is too large (max ~20MB file)"}))  # keep-alive gets corrupted
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, json.dumps({"error": "bad json"}))

        if path == "/discard":
            desk = body.get("agent")
            if desk not in STATE:
                return self._send(400, json.dumps({"error": "unknown desk"}))
            if STATE[desk]["status"] in ("seated", "thinking", "working"):
                return self._send(409, json.dumps({"error": "still running"}))
            git(["reset", "--hard", BASE], cwd=desk_path(desk))
            git(["clean", "-fd"], cwd=desk_path(desk))
            set_(desk, **blank(desk))
            return self._send(200, json.dumps({"ok": True}))

        if path == "/review":
            desk = body.get("agent")
            if desk not in DESKS:
                return self._send(400, json.dumps({"error": "unknown desk"}))
            if not desk_dirty(desk):
                return self._send(409, json.dumps({"error": "nothing to review on this desk"}))
            if STATE[SHIPPER]["status"] in ("seated", "thinking", "working"):
                return self._send(409, json.dumps({"error": "shipper is busy right now"}))
            threading.Thread(target=review_desk, args=(desk,), daemon=True).start()
            return self._send(200, json.dumps({"ok": True}))

        if path == "/ship":
            desk = body.get("agent")
            branches = body.get("branches") or [body.get("branch") or BASE]
            msg = (body.get("message") or "").strip()
            if desk not in DESKS:
                return self._send(400, json.dumps({"error": "unknown desk"}))
            if STATE[desk]["status"] in ("seated", "thinking", "working"):
                return self._send(409, json.dumps({"error": "desk is still running"}))
            if STATE[SHIPPER]["reviewed"] != desk:
                return self._send(409, json.dumps({"error": "run a review on this desk first"}))
            if not msg:
                msg = "%s: %s" % (STATE[desk]["ttype"] or "change", STATE[desk]["task"][:100])
            ok, detail = ship_desk(desk, branches, msg)
            if not ok:
                return self._send(409, json.dumps({"error": detail}))
            set_(desk, **blank(desk))
            set_(SHIPPER, reviewed="", verdict="", status="done",
                 detail=detail, output=detail)
            return self._send(200, json.dumps({"ok": True, "detail": detail}))

        if path == "/stop":
            desk = body.get("agent")
            if desk not in STATE:
                return self._send(400, json.dumps({"error": "unknown desk"}))
            with PROC_LOCK:
                proc = PROC.get(desk)
            if not proc:
                return self._send(409, json.dumps({"error": "this isn't running right now"}))
            STOPPING.add(desk)
            proc.terminate()
            return self._send(200, json.dumps({"ok": True}))

        if path == "/continue":
            desk = body.get("agent")
            task = (body.get("task") or "").strip()
            attachments = body.get("attachments") or []
            if desk not in DESKS:
                return self._send(400, json.dumps({"error": "unknown desk"}))
            if not task:
                return self._send(400, json.dumps({"error": "write the new requirement"}))
            if STATE[desk]["status"] in ("seated", "thinking", "working"):
                return self._send(409, json.dumps({"error": "desk is busy right now"}))
            if not desk_dirty(desk):
                return self._send(409, json.dumps(
                    {"error": "no work on this desk to continue"}))
            threading.Thread(target=run_agent,
                              args=(desk, STATE[desk]["ttype"] or "change", task),
                              kwargs={"attachments": attachments, "resume": True},
                              daemon=True).start()
            return self._send(200, json.dumps({"ok": True}))

        if path == "/upload":
            filename = body.get("filename") or ""
            data_b64 = body.get("data") or ""
            if not data_b64:
                return self._send(400, json.dumps({"error": "file data is empty"}))
            ok, result = save_upload(filename, data_b64)
            if not ok:
                return self._send(400, json.dumps({"error": result}))
            return self._send(200, json.dumps({"ok": True, "path": result,
                                               "filename": os.path.basename(result)}))

        if path == "/transcribe":
            data_b64 = body.get("data") or ""
            if not data_b64:
                return self._send(400, json.dumps({"error": "audio data is empty"}))
            if not voice_ready():
                return self._send(400, json.dumps({"error": "voice input is not set up"}))
            try:
                raw = base64.b64decode(data_b64, validate=True)
            except (ValueError, TypeError):
                return self._send(400, json.dumps({"error": "audio data is invalid"}))
            if len(raw) > 15 * 1024 * 1024:
                return self._send(400, json.dumps({"error": "recording is too long (max 15MB)"}))
            os.makedirs(UPLOADS, exist_ok=True)
            audio_path = os.path.join(UPLOADS, ".rec-%s.webm" % uuid.uuid4().hex[:8])
            with open(audio_path, "wb") as f:
                f.write(raw)
            ok, text = transcribe_audio(audio_path)
            if not ok:
                return self._send(400, json.dumps({"error": text}))
            return self._send(200, json.dumps({"ok": True, "text": translate_to_english(text)}))

        if path == "/rename":
            ok, result = rename_agent(body.get("agent"), body.get("name"))
            if not ok:
                return self._send(400, json.dumps({"error": result}))
            return self._send(200, json.dumps({"ok": True, "name": result}))

        if path == "/suggest-module":
            task = (body.get("task") or "").strip()
            if not task:
                return self._send(400, json.dumps({"error": "write a task first"}))
            guess, why = suggest_module(task)
            return self._send(200, json.dumps({"module": guess, "reason": why}))

        if path != "/run":
            return self._send(404, b"nope", "text/plain")

        task = (body.get("task") or "").strip()
        ttype = body.get("type") or "change"
        module = (body.get("module") or "").strip()
        domain = (body.get("domain") or "").strip()
        attachments = body.get("attachments") or []
        urgent = bool(body.get("urgent"))
        desk = body.get("agent") or free_desk(urgent=urgent)
        if ttype not in TASK_TYPES:
            return self._send(400, json.dumps({"error": "unknown task type"}))
        if not task:
            return self._send(400, json.dumps({"error": "task is empty"}))
        if ttype not in MODULE_OPTIONAL_TYPES:
            if not module:
                return self._send(400, json.dumps(
                    {"error": "choose a module first (or ask AI to find one)"}))
            if module not in list_modules():
                return self._send(400, json.dumps({"error": "no module named %s found" % module}))
        if not desk:
            msg = ("both Urgent Room desks are busy right now" if urgent else
                   "no desk is free - review or discard someone's work first")
            return self._send(409, json.dumps({"error": msg}))
        if desk not in STATE:
            return self._send(400, json.dumps({"error": "unknown desk"}))
        if (desk in URGENT) != urgent:
            return self._send(400, json.dumps(
                {"error": "urgent flag doesn't match the chosen desk"}))
        if STATE[desk]["status"] in ("seated", "thinking", "working"):
            return self._send(409, json.dumps({"error": "this desk is already busy"}))
        if desk_dirty(desk):
            return self._send(409, json.dumps(
                {"error": "%s still has unreviewed work - merge or discard it first" % desk}))
        threading.Thread(target=run_agent, args=(desk, ttype, task),
                          kwargs={"module": module, "domain": domain, "attachments": attachments},
                          daemon=True).start()
        return self._send(200, json.dumps({"ok": True, "desk": desk}))


def selftest():
    assert handle_event({"type": "system", "subtype": "init"})["status"] == "seated"
    w = handle_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "sudo("}}]}})
    assert w["status"] == "working" and w["tool"] == "Grep" and "sudo(" in w["detail"]
    assert handle_event({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "hmm"}]}})["status"] == "thinking"
    d = handle_event({"type": "result", "subtype": "success", "num_turns": 7,
                      "total_cost_usd": 0.42})
    assert d["status"] == "done" and d["turns"] == 7 and d["cost"] == 0.42
    assert handle_event({"type": "result", "subtype": "error_max_turns"})["status"] == "error"
    assert handle_event({"type": "user", "message": {"content": []}}) is None
    long = handle_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "x" * 200}}]}})
    assert len(long["detail"]) == 70, "detail should be truncated"

    # prompt building
    p = build_prompt("module", "  attendance summary  ")
    assert "attendance summary" in p and "Zero Trust" in p
    assert "{task}" not in p, "template wasn't filled in"
    for k in TASK_TYPES:
        assert "{task}" in TASK_TYPES[k]["template"], "%s's template doesn't take task" % k
        assert build_prompt(k, "x").strip()
    try:
        build_prompt("not-a-type", "x")
        raise AssertionError("expected a KeyError for an unknown type")
    except KeyError:
        pass

    # only review should stay read-only, the rest can write
    assert "Edit" not in TASK_TYPES["review"]["tools"]
    assert "Write" not in TASK_TYPES["review"]["tools"]
    for k in ("module", "feature", "change", "fix"):
        assert "Edit" in TASK_TYPES[k]["tools"] and "Write" in TASK_TYPES[k]["tools"]
    # no desk should get shell or git-write access
    for k, v in TASK_TYPES.items():
        for t in v["tools"]:
            assert not t.startswith("Bash(") or t.startswith(
                ("Bash(git diff", "Bash(git log", "Bash(git status", "Bash(git show")), \
                "%s has a dangerous Bash tool: %s" % (k, t)

    assert blank("desk-1")["branch"] == "fleet/desk-1"

    # shipper: never push to main/master, no matter where it sits in the list
    for bl in (["main"], ["master"], ["stage", "main"], ["main", "stage"]):
        ok, why = ship_desk("desk-1", bl, "x")
        assert not ok and "main/master" in why, bl
    ok, why = ship_desk("desk-1", [], "x")
    assert not ok and "no branch" in why
    ok, why = ship_desk("desk-1", ["  ", ""], "x")
    assert not ok and "no branch" in why

    # don't hide it when only part of a push succeeds
    bl = list_branches()
    assert "main" not in bl and "master" not in bl, "main/master shouldn't be in the branch list"
    assert "already pushed" in _partial(["stage"], "prod: conflict")
    assert _partial([], "prod: conflict") == "prod: conflict"

    # review prompt fills in and requires Zero Trust
    rp = REVIEW_PROMPT.format(task="anything")
    assert "anything" in rp and "Zero Trust" in rp and "{task}" not in rp

    assert last_line("a\nb\n", "fb") == "b"
    assert last_line("   ", "fb") == "fb"

    # no agent should have commit/push/shell access
    for k, v in TASK_TYPES.items():
        joined = " ".join(v["tools"])
        for bad in ("commit", "push", "Bash(:*)", "Bash(*)"):
            assert bad not in joined, "%s has %s" % (k, bad)
    assert "Edit" not in READ_TOOLS and "Write" not in READ_TOOLS

    # module: existing-module types need module context, "module" (the new-module type) doesn't
    p1 = build_prompt("feature", "do something", "rs_zk_attendance")
    assert "rs_zk_attendance" in p1 and "Module:" in p1
    p2 = build_prompt("module", "create a new module", "rs_zk_attendance")
    assert "Module:" not in p2, "the new-module type shouldn't need module context"
    p3 = build_prompt("feature", "do something")  # no module given at all
    assert "Module:" not in p3

    # resume/continue prompt reminds about Zero Trust and takes the task
    rsp = RESUME_PROMPT.format(task="add this too")
    assert "add this too" in rsp and "Zero Trust" in rsp and "{task}" not in rsp

    # module list shouldn't crash, and shouldn't suggest a name outside the list
    mods = list_modules()
    assert isinstance(mods, list)

    # new globals for PROC tracking - /stop relies on these
    assert isinstance(PROC, dict) and isinstance(STOPPING, set)

    # Odoo version + domain context gets attached to every write-type prompt
    p1 = build_prompt("feature", "do something", "rs_zk_attendance", "Attendance/HR")
    assert "Domain: Attendance/HR" in p1 and ("Odoo %s" % ODOO_VERSION) in p1
    p2 = build_prompt("module", "new module")
    assert "Domain:" not in p2 and ("Odoo %s" % ODOO_VERSION) in p2, "a new module should also know the Odoo version"

    # every desk has a name, the urgent room is a separate pool but still part of DESKS
    assert all(NAMES.get(d) for d in DESKS), "every desk should have a name"
    assert URGENT.issubset(set(DESKS)) and len(URGENT) == 2
    assert blank("urgent-1")["urgent"] is True and blank("desk-1")["urgent"] is False

    # free_desk() should never mix the urgent and normal pools
    assert free_desk(urgent=True) in URGENT or free_desk(urgent=True) is None
    assert free_desk(urgent=False) not in URGENT

    # triage fail-safe: even if the subprocess fails, no crash - falls back to sonnet+non-urgent
    _real_run = subprocess.run
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=45)
    subprocess.run = _boom
    try:
        model, urgent_flag = triage_task("feature", "anything", "")
        assert model == "sonnet" and urgent_flag is False
    finally:
        subprocess.run = _real_run

    # --- uploads ---
    import base64 as _b64
    ok, path = save_upload("report.pdf", _b64.b64encode(b"hello").decode())
    assert ok and path.endswith("-report.pdf") and os.path.isfile(path)
    with open(path, "rb") as f:
        assert f.read() == b"hello"
    os.remove(path)

    ok, err = save_upload("../../etc/passwd.png", _b64.b64encode(b"x").decode())
    assert ok, "basename should still be extracted correctly after path traversal"
    assert ".." not in path and os.path.dirname(path) == UPLOADS
    if ok:
        os.remove(err)  # 'err' is actually the path here (ok=True), clean it up

    ok, err = save_upload("virus.exe", _b64.b64encode(b"x").decode())
    assert not ok and "not supported" in err

    ok, err = save_upload("big.png", _b64.b64encode(b"x" * (UPLOAD_MAX_BYTES + 1)).decode())
    assert not ok and "over the" in err

    ok, err = save_upload("bad.png", "not-valid-base64!!")
    assert not ok and "corrupt" in err

    note = attachment_note([{"path": "/tmp/a.pdf"}, {"path": "/tmp/b.png"}])
    assert "/tmp/a.pdf" in note and "/tmp/b.png" in note
    assert attachment_note([]) == "" and attachment_note(None) == ""

    # --- desk names + rename persistence ---
    assert set(DEFAULT_NAMES) == set(DESKS)
    assert len(set(DEFAULT_NAMES.values())) == len(DEFAULT_NAMES), "every name should be different"
    _orig_names_file = NAMES_FILE
    _tmp_names_file = _orig_names_file + ".selftest-tmp"
    globals()["NAMES_FILE"] = _tmp_names_file
    try:
        NAMES["desk-1"] = "TestBot"
        save_names()
        reloaded = load_names()
        assert reloaded["desk-1"] == "TestBot"
        assert reloaded["desk-2"] == DEFAULT_NAMES["desk-2"], "the rest of the names should stay default"
    finally:
        NAMES["desk-1"] = DEFAULT_NAMES["desk-1"]
        globals()["NAMES_FILE"] = _orig_names_file
        if os.path.exists(_tmp_names_file):
            os.remove(_tmp_names_file)

    # rename_agent - this exact call used to crash set_() with 'multiple values
    # for argument name' (set_'s first param is itself called 'name')
    globals()["NAMES_FILE"] = _tmp_names_file
    try:
        ok, result = rename_agent("desk-2", "  RenameTest  ")
        assert ok and result == "RenameTest"
        assert STATE["desk-2"]["name"] == "RenameTest" and NAMES["desk-2"] == "RenameTest"
        ok, err = rename_agent("desk-2", "   ")
        assert not ok and "empty" in err
        ok, err = rename_agent("nonexistent-desk", "x")
        assert not ok and "unknown" in err
    finally:
        NAMES["desk-2"] = DEFAULT_NAMES["desk-2"]
        STATE["desk-2"]["name"] = DEFAULT_NAMES["desk-2"]
        globals()["NAMES_FILE"] = _orig_names_file
        if os.path.exists(_tmp_names_file):
            os.remove(_tmp_names_file)

    # --- voice input ---
    assert ARABIC_SCRIPT_RE.search("اردو"), "should detect Arabic script"
    assert not ARABIC_SCRIPT_RE.search("plain english text"), "Latin script shouldn't false-positive"
    assert translate_to_english("plain english text") == "plain english text", \
        "text should come back unchanged when there's no Arabic script"

    _real_bin = WHISPER_BIN
    globals()["WHISPER_BIN"] = "/nonexistent/whisper-cli"
    try:
        assert voice_ready() is False
        ok, err = transcribe_audio("/nonexistent/audio.webm")
        assert not ok and "not set up" in err
    finally:
        globals()["WHISPER_BIN"] = _real_bin

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        raise SystemExit(0)
    if not os.path.isdir(PROJECT):
        sys.exit("FLEET_PROJECT does not exist: %s\n"
                  "Set it to your Odoo custom_addons checkout, e.g.:\n"
                  "  export FLEET_PROJECT=/path/to/custom_addons" % PROJECT)
    print("Agent Town  ->  http://127.0.0.1:%d   (project: %s, base: %s)" % (PORT, PROJECT, BASE))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
