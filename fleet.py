#!/usr/bin/env python3
"""Agent Town - local dashboard for Claude Code subagents.

Each desk has its own git worktree, so several agents can write at once
without stepping on each other. Run:  python3 fleet.py  ->  http://127.0.0.1:8765
"""
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

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

DESKS = ["desk-1", "desk-2", "desk-3", "desk-4", "desk-5", "urgent-1", "urgent-2"]
# urgent-1/2 have their own "room" - always kept empty so real urgent work
# never has to queue behind normal work. Fast model, AI triage skipped too.
URGENT = {"urgent-1", "urgent-2"}
DEFAULT_NAMES = {
    "desk-1": "Byte", "desk-2": "Cortex", "desk-3": "Vector",
    "desk-4": "Nova", "desk-5": "Quark",
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


# ---------------------------------------------------------------- token ledger
# Every `claude` call reports exactly what it burned in its final `result`
# event, so the ledger below is measured, never estimated. It survives a
# restart in usage.json, same pattern as names.json.
USAGE_FILE = os.path.join(HERE, "usage.json")
TOKEN_KEYS = ("in", "out", "cache_read", "cache_write")


def token_bucket():
    b = {k: 0 for k in TOKEN_KEYS}
    b.update(cost=0.0, runs=0)
    return b


def load_usage():
    try:
        with open(USAGE_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    fields = set(token_bucket())
    merge = lambda d: dict(token_bucket(), **{k: v for k, v in (d or {}).items()
                                              if k in fields})
    return {"day": saved.get("day") or time.strftime("%Y-%m-%d"),
            "today": merge(saved.get("today")),
            "total": merge(saved.get("total")),
            "models": {k: merge(v) for k, v in (saved.get("models") or {}).items()}}


USAGE = load_usage()
USAGE_LOCK = threading.Lock()


def save_usage():
    try:
        with open(USAGE_FILE, "w") as f:
            json.dump(USAGE, f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- per-run history
# USAGE above is rolling sums (today/total/per-model) - fine for "how much
# have I burned", useless for "which task cost me a dollar". This is the
# same numbers, one row per completed run, so the usage panel can answer
# that. Capped and trimmed on every write - it's a recent-activity log, not
# an audit trail.
HISTORY_FILE = os.path.join(HERE, "usage_history.json")
HISTORY_MAX = 300


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


HISTORY = load_history()


def save_history():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(HISTORY[-HISTORY_MAX:], f, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- activity log + debug log file
# Two outputs from one call: an in-UI Activity panel (events.json, capped,
# same pattern as HISTORY above) and a rotating plain-text log file for
# debugging after the fact - a crash or a silent failure is otherwise
# invisible once the browser tab is closed. stdlib logging only, no new
# dependency: RotatingFileHandler caps the file itself so it never grows
# unbounded on a server left running for weeks.
LOG_FILE = os.path.join(HERE, "fleet.log")
logger = logging.getLogger("agent_town")
logger.setLevel(logging.INFO)
if not logger.handlers:  # importing this module twice (e.g. selftest) must not double-log
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)

EVENTS_FILE = os.path.join(HERE, "events.json")
EVENTS_MAX = 300


def load_events():
    try:
        with open(EVENTS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


EVENTS = load_events()
EVENTS_LOCK = threading.Lock()


def save_events():
    try:
        with open(EVENTS_FILE, "w") as f:
            json.dump(EVENTS[-EVENTS_MAX:], f, indent=2)
    except OSError:
        pass


def log_event(kind, desk, message, level="info"):
    """kind is a short machine tag (task_start/task_done/task_error/review/
    ship/push/pull/discard/...), desk may be None for dashboard-wide events."""
    getattr(logger, level, logger.info)("[%s] %s: %s" % (kind, desk or "-", message))
    with EVENTS_LOCK:
        EVENTS.append({"ts": time.time(), "kind": kind, "desk": desk,
                       "message": message, "level": level})
        del EVENTS[:-EVENTS_MAX]
        save_events()


def event_tokens(usage):
    """The four token buckets out of a stream-json `usage` block.

    Cache reads are billed far cheaper than fresh input, so they are kept
    apart instead of lumped into one "input" number - the split is the whole
    point of the efficiency readout. Checked by selftest."""
    u = usage or {}
    return {"in": u.get("input_tokens") or 0,
            "out": u.get("output_tokens") or 0,
            "cache_read": u.get("cache_read_input_tokens") or 0,
            "cache_write": u.get("cache_creation_input_tokens") or 0}


def track_usage(ev, desk=None):
    """Fold one stream-json event into the desk's live counters and the
    fleet ledger.

    Every claude invocation routes through here - including the small helper
    calls (triage, translate, suggest, review), which are easy to forget but
    are not free.

    `assistant` events give a live running count while the desk works; the
    `result` event at the end carries the authoritative total for the whole
    run, so it replaces the running count rather than adding to it."""
    t = ev.get("type")
    if t == "assistant" and desk:
        tk = event_tokens((ev.get("message") or {}).get("usage"))
        with LOCK:
            live = STATE[desk]["tokens"]
            for k in TOKEN_KEYS:
                live[k] += tk[k]
        return
    if t != "result":
        return
    tk = event_tokens(ev.get("usage"))
    cost = ev.get("total_cost_usd") or 0.0
    if desk:
        set_(desk, tokens=tk)
    today = time.strftime("%Y-%m-%d")
    with USAGE_LOCK:
        if USAGE["day"] != today:
            USAGE["day"], USAGE["today"] = today, token_bucket()
        for b in (USAGE["today"], USAGE["total"]):
            for k in TOKEN_KEYS:
                b[k] += tk[k]
            b["cost"] += cost
            b["runs"] += 1
        for model, mu in (ev.get("modelUsage") or {}).items():
            m = USAGE["models"].setdefault(model, token_bucket())
            m["in"] += mu.get("inputTokens") or 0
            m["out"] += mu.get("outputTokens") or 0
            m["cache_read"] += mu.get("cacheReadInputTokens") or 0
            m["cache_write"] += mu.get("cacheCreationInputTokens") or 0
            m["cost"] += mu.get("costUSD") or 0.0
            m["runs"] += 1
        save_usage()
        if desk:
            st = STATE.get(desk) or {}
            HISTORY.append({"ts": time.time(), "desk": desk, "name": st.get("name") or desk,
                            "task": (st.get("task") or "")[:120], "ttype": st.get("ttype") or "",
                            "model": st.get("model") or "", "tokens": tk, "cost": cost,
                            "turns": ev.get("num_turns") or 0})
            del HISTORY[:-HISTORY_MAX]
            save_history()


def derive(b):
    """Add the numbers the UI reads off a raw bucket. Checked by selftest."""
    served = b["in"] + b["cache_read"] + b["cache_write"]
    b = dict(b, total=served + b["out"])
    b["cache_hit"] = round(100.0 * b["cache_read"] / served, 1) if served else 0.0
    b["per_run"] = int(b["total"] / b["runs"]) if b["runs"] else 0
    b["cost_per_run"] = round(b["cost"] / b["runs"], 4) if b["runs"] else 0.0
    return b


_PLAN = {}


def plan_info():
    """Who is paying, straight from the CLI.

    A claude.ai subscription has no credit balance to report - usage is
    included in the plan and the CLI exposes no remaining-quota number - so
    the panel shows the plan and the notional API-rate cost instead of
    inventing a "credits left" figure."""
    if _PLAN:
        return _PLAN
    try:
        r = subprocess.run(["claude", "auth", "status"], capture_output=True,
                           text=True, timeout=20)
        d = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"plan": "", "auth": "", "email": ""}   # retry on the next poll
    _PLAN.update({"plan": d.get("subscriptionType") or d.get("apiProvider") or "",
                  "auth": d.get("authMethod") or "",
                  "email": d.get("email") or ""})
    return _PLAN


def usage_report():
    with USAGE_LOCK:
        u = json.loads(json.dumps(USAGE))
    return {"day": u["day"], "today": derive(u["today"]), "total": derive(u["total"]),
            "models": {k: derive(v) for k, v in sorted(u["models"].items())},
            "plan": plan_info()}


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
            "log": [], "output": "", "branch": "fleet/%s" % name, "changed": 0,
            "tokens": {k: 0 for k in TOKEN_KEYS}}


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


def desk_unpushed(name):
    """Committed work on this desk that never reached any origin branch.

    A half-finished ship (commit done, cherry-pick failed) leaves the worktree
    clean with the commit stranded on fleet/<desk>. Without this, the desk
    looks free and the work is silently lost."""
    path = desk_path(name)
    if not os.path.isdir(path):
        return False
    rc, out = git(["rev-list", "--count", "HEAD", "--not", "--remotes=origin"], cwd=path)
    return rc == 0 and out.strip() not in ("", "0")


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


# ---------- in-browser file explorer / editor (scoped to a desk's worktree) ----------

FILE_EXPLORE_MAX_BYTES = 1_000_000  # plenty for any real source file, small for the browser


def desk_file_path(desk, rel_path):
    """Resolve rel_path inside desk's worktree, refusing traversal outside it.
    Returns an absolute path, or None if the desk is unknown or the path escapes."""
    if desk not in DESKS:
        return None
    root = os.path.realpath(desk_path(desk))
    target = os.path.realpath(os.path.join(root, rel_path or ""))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def list_desk_files(desk):
    """Tracked + untracked-but-not-ignored files in the desk's worktree, sorted."""
    root = desk_path(desk)
    if not os.path.isdir(root):
        return []
    rc, out = git(["ls-files", "--cached", "--others", "--exclude-standard"], cwd=root)
    if rc != 0:
        return []
    return sorted(l for l in out.splitlines() if l.strip())


def read_desk_file(desk, rel_path):
    """Returns (ok, content_or_error, truncated)."""
    path = desk_file_path(desk, rel_path)
    if not path or not os.path.isfile(path):
        return False, "file not found", False
    try:
        with open(path, "rb") as f:
            raw = f.read(FILE_EXPLORE_MAX_BYTES + 1)
    except OSError as e:
        return False, str(e), False
    if b"\x00" in raw:
        return False, "binary file - can't edit here", False
    truncated = len(raw) > FILE_EXPLORE_MAX_BYTES
    return True, raw[:FILE_EXPLORE_MAX_BYTES].decode("utf-8", "replace"), truncated


def write_desk_file(desk, rel_path, content):
    """Returns (ok, error_or_none)."""
    path = desk_file_path(desk, rel_path)
    if not path:
        return False, "invalid path"
    if not os.path.isdir(os.path.dirname(path)):
        return False, "directory doesn't exist"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return False, str(e)
    return True, None


# ---------- structured diff (for the inline per-file diff viewer) ----------

def parse_unified_diff(diff_text):
    """`git diff` output -> [{"file", "hunks":[{"header","lines":[(sign,text)]}]}].
    Pure text parsing - git already computed the diff, this just groups it by
    file/hunk so the UI can color +/- lines without shelling out again."""
    files = []
    cur = hunk = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+) b/(.+)$", line)
            cur = {"file": m.group(2) if m else line[11:], "hunks": [], "binary": False}
            files.append(cur)
            hunk = None
        elif line.startswith("Binary files"):
            if cur:
                cur["binary"] = True
        elif line.startswith("@@"):
            hunk = {"header": line, "lines": []}
            if cur is not None:
                cur["hunks"].append(hunk)
        elif line.startswith(("+++ ", "--- ", "index ", "\\")):
            continue  # "\ No newline at end of file" and file-header lines
        elif hunk is not None:
            sign = line[:1] if line[:1] in ("+", "-") else " "
            hunk["lines"].append([sign, line[1:]])
    return files


def desk_diff_json(desk):
    """Tracked changes (via git diff) plus untracked new files (shown as all-added)."""
    root = desk_path(desk)
    rc, out = git(["diff", "HEAD"], cwd=root)
    files = parse_unified_diff(out) if rc == 0 else []
    rc2, untracked = git(["ls-files", "--others", "--exclude-standard"], cwd=root)
    for rel in (l for l in untracked.splitlines() if l.strip()) if rc2 == 0 else ():
        ok, content, _ = read_desk_file(desk, rel)
        if not ok:
            files.append({"file": rel, "binary": True,
                          "hunks": [{"header": "new file", "lines": [["+", "(binary or unreadable)"]]}]})
            continue
        files.append({"file": rel, "binary": False,
                      "hunks": [{"header": "@@ new file @@",
                                "lines": [["+", l] for l in content.splitlines()]}]})
    return files


def default_commit_message(desk):
    """Descriptive fallback commit message for when the Push box is left
    empty - task text plus what actually changed (from `git diff --stat`),
    not just a static "change: <task>" label. No AI call - git already
    knows exactly what changed, this just reads it.

    Git UIs (odoo.sh included) only show a commit's first line inline -
    the rest is body text you only see on hover/expand. So when there's no
    task text to describe the work (e.g. a direct in-browser file edit,
    never "Assign"-ed), the changed file names go straight into that first
    line too, not just the body - otherwise the list view shows a useless
    bare "Change / Update" with the real info hidden behind a hover."""
    ttype = STATE[desk]["ttype"] or "change"
    task = (STATE[desk]["task"] or "").strip()
    label = TASK_TYPES.get(ttype, {}).get("label", ttype)
    root = desk_path(desk)
    rc, stat = git(["diff", "--stat", "HEAD"], cwd=root)
    changed = [l.split("|")[0].strip() for l in stat.splitlines() if "|" in l] if rc == 0 else []
    rc2, untracked = git(["ls-files", "--others", "--exclude-standard"], cwd=root)
    added = [l.strip() for l in untracked.splitlines() if l.strip()] if rc2 == 0 else []
    files = changed + added
    if task:
        subject = "%s: %s" % (label, task[:80])
    elif files:
        preview = ", ".join(files[:3]) + (" +%d more" % (len(files) - 3) if len(files) > 3 else "")
        subject = "%s: %s" % (label, preview)
    else:
        subject = label
    if not files:
        return subject
    preview = ", ".join(files[:6]) + (" +%d more" % (len(files) - 6) if len(files) > 6 else "")
    return "%s\n\n%d file(s) changed: %s" % (subject, len(files), preview)


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
                 turns=0, cost=0.0, tokens={k: 0 for k in TOKEN_KEYS},
                 report=None, started=time.time(), log=[],
                 output="", changed=0)
            model_key, ai_urgent = triage_task(ttype, task, domain)

    set_(name, status="seated", task=display_task, ttype=ttype, domain=domain,
         model=model_key, urgent=urgent, ai_urgent=ai_urgent,
         tool="", detail="preparing worktree", turns=0, cost=0.0, report=None,
         tokens={k: 0 for k in TOKEN_KEYS},
         started=time.time(), log=[], output="", changed=0)
    log_event("task_start", name, "%s: %s" % (ttype, display_task[:100]))

    if not resume:
        ok, msg = ensure_worktree(name)
        if not ok:
            set_(name, status="error", detail=msg)
            log_event("task_error", name, "worktree setup failed: %s" % msg, level="error")
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
        log_event("task_error", name, "claude CLI not found: %s" % e, level="error")
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
        track_usage(ev, name)
        patch = handle_event(ev)
        if patch:
            set_(name, **patch)
            if patch.get("tool"):
                with LOCK:
                    log = STATE[name]["log"]
                    log.append({"t": time.time(), "tool": patch["tool"],
                                "detail": patch["detail"]})
                    del log[:-60]
            if patch.get("status") in ("done", "error"):
                log_event("task_" + patch["status"], name, patch.get("detail") or "",
                          level="info" if patch["status"] == "done" else "error")

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
        log_event("task_stopped", name, "stopped by user")
        return

    if p.returncode != 0 and not final:
        tail = err.strip() or "exit %d" % p.returncode
        set_(name, status="error", detail=tail.splitlines()[-1][:120], output=tail[-4000:])
        log_event("task_error", name, tail.splitlines()[-1][:200], level="error")
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


def _raw_branches():
    """All remote branches (including main/master), deduped. One cached git
    call shared by both list_branches() and list_all_branches() below."""
    now = time.time()
    if now - _branch_cache[0] < _BRANCH_TTL and _branch_cache[1]:
        return _branch_cache[1]
    rc, out = git(["for-each-ref", "--sort=-committerdate", "--format=%(refname:lstrip=3)",
                   "refs/remotes/origin"])
    names = []
    for line in out.splitlines():
        b = line.strip()
        if not b or b == "HEAD" or b in names:
            continue
        names.append(b)
    _branch_cache[0], _branch_cache[1] = now, names
    return names


def list_branches():
    """Push-target branches - excludes main/master, since ship_desk() refuses
    to push straight to either of those."""
    return [b for b in _raw_branches() if b not in ("main", "master")]


def list_all_branches():
    """Every branch, including main/master - used for the base-branch picker,
    where starting new work from main is perfectly reasonable."""
    return _raw_branches()


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
        track_usage(ev)
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
        track_usage(ev)
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
        track_usage(ev)
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
                if not desk_dirty(n) and not desk_unpushed(n):
                    return n
    return None


REVIEW_PROMPT = (
    "There is unshipped work in this worktree. Review it.\n\n"
    "The original task that was given: {task}\n\n"
    "Run `git status` and `git diff` to see what changed. New (untracked) "
    "files won't show up in git diff - read those with Read. If the working "
    "tree turns out to be clean, the work is already committed but not yet "
    "pushed - `git log -p --not --remotes=origin` shows it.\n\n"
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


# ---------------------------------------------------------------- self-git
# Agent Town's own source (this file, index.html, ...) lives in a plain git
# checkout at HERE, tracking origin/<branch> on its own repo - completely
# separate from the per-desk worktrees above, which point at the Odoo
# project being worked on. This is deliberately simple: one branch, no
# cherry-picks, no conflict machinery - just add/commit/push/pull, the way
# anyone would do it by hand, so the dashboard's own development doesn't
# require leaving the dashboard.
SELF_TRACKED = ("fleet.py", "index.html", "test_ui.js", "names.json",
                "README.md", "LICENSE", ".gitignore", "setup-voice.sh")


def self_git(args):
    return git(args, cwd=HERE)


def self_branch():
    rc, out = self_git(["rev-parse", "--abbrev-ref", "HEAD"])
    return out.strip() if rc == 0 else ""


def repo_status(path, tracked=None, do_fetch=False, dirty_cap=50):
    """Generic git status for any repo - branch/remote/dirty/ahead-behind/
    last-commit. Never raises - a repo with no commits yet or no remote
    just reports empty/zero fields.

    tracked=None means "the whole tree" (used for the read-only Project
    view, which isn't scoped to a curated file list); a tuple restricts to
    those pathspecs (used for self-git, which only ever touches its own
    known files). dirty is capped for the response payload; dirty_count is
    always the true total.

    ahead/behind only reflect what was known as of the last `git fetch` -
    do_fetch=True updates that first (a network round-trip, so callers only
    ask for it on an explicit check, not on every cheap header-chip poll)."""
    def rg(args):
        return git(args, cwd=path)
    if do_fetch:
        rg(["fetch", "origin"])
    rc, branch = rg(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch.strip() if rc == 0 else ""
    rc2, remote_url = rg(["remote", "get-url", "origin"])
    rc3, out = rg(["status", "--porcelain"] + (list(tracked) if tracked else []))
    dirty_all = [l[3:].strip() for l in out.splitlines() if l.strip()] if rc3 == 0 else []
    rc4, log = rg(["log", "-1", "--format=%h\x1f%s\x1f%ar\x1f%an"])
    h, subj, when, author = (log.strip().split("\x1f") + ["", "", "", ""])[:4] if rc4 == 0 and log.strip() else ("", "", "", "")
    ahead = behind = 0
    if branch:
        rc5, counts = rg(["rev-list", "--left-right", "--count", "HEAD...origin/%s" % branch])
        parts = counts.split() if rc5 == 0 else []
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    return {"branch": branch or "?", "remote": remote_url.strip() if rc2 == 0 else "",
            "dirty": dirty_all[:dirty_cap], "dirty_count": len(dirty_all),
            "ahead": ahead, "behind": behind,
            "last_commit": {"hash": h, "subject": subj, "when": when, "author": author}}


def self_git_status(do_fetch=False):
    return repo_status(HERE, SELF_TRACKED, do_fetch=do_fetch)


def project_git_status(do_fetch=False):
    """Read-only - the dashboard never commits/pushes/switches branches on
    the user's own Project checkout, only its own repo and the desk
    worktrees. This is purely informational (plus the full branch list, for
    "which branches exist" at a glance)."""
    st = repo_status(PROJECT, None, do_fetch=do_fetch)
    st["branches"] = list_all_branches()
    return st


def self_default_commit_message():
    rc, out = self_git(["status", "--porcelain"] + list(SELF_TRACKED))
    files = [l[3:].strip() for l in out.splitlines() if l.strip()] if rc == 0 else []
    if not files:
        return "Update Agent Town"
    preview = ", ".join(files[:4]) + (" +%d more" % (len(files) - 4) if len(files) > 4 else "")
    return "Update: %s" % preview


def self_commit_and_push(message):
    """Commit any dirty tracked files (never the .bak/loose files that live
    alongside them) and push to origin/<current branch>.

    Unlike the desk-ship flow, this does NOT refuse main/master - Agent
    Town's own repo is small and single-branch, and pushing straight to
    main is its actual, real workflow (unlike the Odoo project's
    stage/saad-dev gating, which desk-ship exists to protect)."""
    branch = self_branch()
    if not branch:
        return False, "couldn't determine the current branch"
    rc, out = self_git(["status", "--porcelain"] + list(SELF_TRACKED))
    if rc != 0:
        return False, last_line(out, "git status failed")
    if out.strip():
        # explicit pathspecs make `git add` fail outright if even one is
        # missing (e.g. setup-voice.sh never existed on this checkout) -
        # only add the ones actually present, same effect either way
        existing = [f for f in SELF_TRACKED if os.path.isfile(os.path.join(HERE, f))]
        rc, out = self_git(["add"] + existing) if existing else (0, "")
        if rc:
            return False, last_line(out, "git add failed")
        msg = (message or "").strip() or self_default_commit_message()
        rc, out = self_git(["commit", "-m", msg])
        if rc:
            return False, last_line(out, "git commit failed")
    rc, ahead_out = self_git(["rev-list", "--count", "origin/%s..HEAD" % branch])
    if rc == 0 and ahead_out.strip() == "0":
        return False, "nothing to push - already up to date with origin"
    rc, out = self_git(["push", "origin", branch])
    if rc:
        return False, last_line(out, "git push failed")
    return True, "pushed to origin/%s" % branch


def self_pull():
    """Fast-forward only - refuses rather than create a surprise merge
    commit, and refuses while there are uncommitted changes so a pull can
    never clobber work in progress."""
    rc, out = self_git(["status", "--porcelain"] + list(SELF_TRACKED))
    if rc == 0 and out.strip():
        return False, "commit or discard your changes before pulling"
    branch = self_branch()
    rc, out = self_git(["fetch", "origin"])
    if rc:
        return False, last_line(out, "git fetch failed")
    rc, out = self_git(["pull", "--ff-only", "origin", branch] if branch else ["pull", "--ff-only"])
    if rc:
        return False, last_line(out, "git pull failed (diverged from origin?)")
    return True, (last_line(out, "already up to date") + " - restart the server to run the new code")


def review_desk(desk):
    """Read-only review of a desk's work by the shipper. Doesn't push anything."""
    task = STATE[desk]["task"] or "(task wasn't recorded)"
    set_(SHIPPER, status="seated", task="review: %s" % desk, ttype="review",
         tool="", detail="reading the diff", started=time.time(), log=[],
         output="", turns=0, cost=0.0, tokens={k: 0 for k in TOKEN_KEYS},
         reviewed=desk, verdict="")
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
        track_usage(ev, SHIPPER)
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
        log_event("review_stopped", desk, "review stopped by user")
        return
    if pr.returncode != 0 and not final:
        set_(SHIPPER, status="error", detail=last_line(err, "exit %d" % pr.returncode),
             output=err[-4000:])
        log_event("review_error", desk, last_line(err, "exit %d" % pr.returncode), level="error")
        return
    set_(SHIPPER, output=final or "(no output)", verdict=final or "")
    log_event("review", desk, (final or "(no output)").splitlines()[0][:150])


# A cherry-pick that hits a conflict is PAUSED here, not aborted - aborting
# throws away the only place the conflict can actually be resolved, which is
# what forced people into the terminal. The worktree stays mid-cherry-pick
# until the user resolves it from the UI (or gives up and aborts).
#
# Persisted, because the paused pick outlives this process: git keeps it in
# the worktree, so a restart that only cleared an in-memory dict would strand
# the work with no way back to it from the UI.
CONFLICTS_FILE = os.path.join(HERE, "conflicts.json")


def conflict_files(desk):
    """Files git left with conflict markers, i.e. unmerged in the index."""
    rc, out = git(["diff", "--name-only", "--diff-filter=U"], cwd=desk_path(desk))
    return sorted(f for f in out.splitlines() if f.strip()) if rc == 0 else []


def cherry_pick_paused(desk):
    """Is git itself still mid-cherry-pick on this desk? git is the source of
    truth here; our JSON only remembers the parts git doesn't know (which
    branch we were pushing onto, and which are still queued)."""
    rc, _ = git(["rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD"],
                cwd=desk_path(desk))
    return rc == 0


def load_conflicts():
    """Reload paused conflicts, dropping any that git has since finished or
    that were aborted from a terminal - a stale entry would block its desk
    forever."""
    try:
        with open(CONFLICTS_FILE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return {}
    return {d: c for d, c in saved.items()
            if d in DESKS and isinstance(c, dict) and cherry_pick_paused(d)}


def save_conflicts():
    try:
        with open(CONFLICTS_FILE, "w") as f:
            json.dump(CONFLICTS, f, indent=2)
    except OSError:
        pass


CONFLICTS = load_conflicts()


CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


def has_markers(desk, rel_path):
    """Are git's conflict markers still sitting in this file? Checked by
    selftest - committing a file with <<<<<<< in it is the one mistake that
    is painful to undo after the push."""
    target = desk_file_path(desk, rel_path)
    if not target or not os.path.isfile(target):
        return False
    try:
        with open(target, encoding="utf-8", errors="replace") as f:
            return any(line.startswith(CONFLICT_MARKERS) for line in f)
    except OSError:
        return False


def conflict_state(desk):
    """What the UI needs to render the resolver, or None when there's nothing
    to resolve. Rebuilds the file list live so it shrinks as files are fixed."""
    c = CONFLICTS.get(desk)
    if not c:
        return None
    return dict(c, files=conflict_files(desk))


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
    if desk in CONFLICTS:
        return False, "this desk is paused on a merge conflict - resolve it first"
    # A previous ship can stop half way (commit went through, then a
    # cherry-pick failed): the worktree is clean but the commit never landed
    # on a branch. Don't commit again in that case - ship what HEAD already has.
    if desk_dirty(desk):
        rc, out = git(["add", "-A"], cwd=path)
        if rc:
            return False, last_line(out, "git add failed")
        rc, out = git(["commit", "-m", message], cwd=path)
        if rc:
            return False, last_line(out, "git commit failed")
    elif not desk_unpushed(desk):
        return False, "no changes on this desk"
    rc, sha = git(["rev-parse", "HEAD"], cwd=path)
    return push_branches(desk, sha.strip(), branches, [])


def push_branches(desk, sha, pending, done):
    """Cherry-pick `sha` onto each pending branch and push it.

    Split out of ship_desk so a conflict can pause the run and
    resolve_conflict() can resume this very loop with the branches that are
    still left."""
    path, home = desk_path(desk), "fleet/%s" % desk
    pending = list(pending)
    while pending:
        br = pending.pop(0)
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
            files = conflict_files(desk)
            if not files:      # failed for some other reason - nothing to resolve
                git(["cherry-pick", "--abort"], cwd=path)
                git(["checkout", home], cwd=path)
                return False, _partial(done, "%s: cherry-pick failed - %s"
                                       % (br, last_line(out, "")))
            CONFLICTS[desk] = {"branch": br, "sha": sha, "done": done,
                               "pending": pending, "files": files}
            save_conflicts()
            return False, _partial(done, "%s: %d file(s) conflict - resolve them below"
                                   % (br, len(files)))
        rc, out = git(["push", "origin", "HEAD:%s" % br], cwd=path)
        if rc:
            git(["checkout", home], cwd=path)
            return False, _partial(done, "%s: push failed - %s" % (br, last_line(out, "")))
        done.append(br)

    git(["checkout", home], cwd=path)
    # The cherry-picked commits get new shas on the target branches, so the
    # original one on fleet/<desk> would look un-pushed forever and the desk
    # would never be handed out again. The work is safely on the branches
    # now, so wind the desk back to a clean base.
    git(["reset", "--hard", BASE], cwd=path)
    return True, "%s -> %s pushed" % (desk, " + ".join("origin/" + b for b in done))


def resolve_conflict(desk, action, rel_path=""):
    """Drive a paused cherry-pick from the UI.

    take-ours / take-theirs are per-file shortcuts for the common case;
    anything subtler is edited in the file editor, which already writes into
    this same worktree. "continue" refuses while markers remain, because
    committing a file with <<<<<<< in it is the one mistake that is painful
    to undo after the push."""
    c = CONFLICTS.get(desk)
    if not c:
        return False, "no conflict is waiting on this desk"
    path, home = desk_path(desk), "fleet/%s" % desk

    if action == "abort":
        git(["cherry-pick", "--abort"], cwd=path)
        git(["checkout", home], cwd=path)
        CONFLICTS.pop(desk, None)
        save_conflicts()
        return True, _partial(c["done"], "conflict aborted - nothing pushed to %s" % c["branch"])

    if action in ("ours", "theirs"):
        if rel_path not in conflict_files(desk):
            return False, "that file isn't one of the conflicted ones"
        # During a cherry-pick "ours" is the target branch and "theirs" is the
        # desk's own commit - the reverse of what people expect, hence the
        # UI wording rather than the git wording.
        rc, out = git(["checkout", "--%s" % action, "--", rel_path], cwd=path)
        if rc:
            return False, last_line(out, "could not take that version")
        rc, out = git(["add", "--", rel_path], cwd=path)
        if rc:
            return False, last_line(out, "git add failed")
        return True, "%s: kept the %s version" % (rel_path,
                                                  "desk's" if action == "theirs" else "branch's")

    if action != "continue":
        return False, "unknown action"

    # Editing a file in the UI clears the markers but leaves the index entry
    # unmerged - only `git add` clears that. So the gate is the markers in the
    # working tree, not the index, and the add comes after.
    left = [f for f in c["files"] if has_markers(desk, f)]
    if left:
        return False, "conflict markers are still in: %s" % ", ".join(left[:5])
    rc, out = git(["add", "-A"], cwd=path)
    if rc:
        return False, last_line(out, "git add failed")
    still = conflict_files(desk)
    if still:
        return False, "git still calls these unmerged: %s" % ", ".join(still[:5])
    # core.editor=true: --continue wants to open an editor for the message,
    # and there is no terminal here to open one in.
    rc, out = git(["-c", "core.editor=true", "cherry-pick", "--continue"], cwd=path)
    if rc and "no cherry-pick" not in out.lower():
        return False, last_line(out, "cherry-pick --continue failed")
    rc, out = git(["push", "origin", "HEAD:%s" % c["branch"]], cwd=path)
    if rc:
        return False, _partial(c["done"], "%s: push failed - %s"
                               % (c["branch"], last_line(out, "")))
    CONFLICTS.pop(desk, None)
    save_conflicts()
    return push_branches(desk, c["sha"], c["pending"], c["done"] + [c["branch"]])


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
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        query = urllib.parse.parse_qs(split.query)
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
                                               "usage": usage_report(),
                                               "conflicts": {d: conflict_state(d)
                                                             for d in CONFLICTS},
                                               "now": time.time()}))
        if path == "/branches":
            names = list_branches()
            # pre-checked by default - the user usually wants both base and stage
            return self._send(200, json.dumps({"branches": names,
                                               "default": list(dict.fromkeys(
                                                   b for b in (BASE, "stage") if b in names))}))
        if path == "/base-branches":
            return self._send(200, json.dumps({"branches": list_all_branches(), "base": BASE}))
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
        if path.startswith("/diff-json/"):
            n = os.path.basename(path[len("/diff-json/"):])
            if n not in STATE:
                return self._send(404, b"unknown desk", "text/plain")
            return self._send(200, json.dumps({"files": desk_diff_json(n)}))
        if path.startswith("/commit-message/"):
            n = os.path.basename(path[len("/commit-message/"):])
            if n not in DESKS:
                return self._send(404, b"unknown desk", "text/plain")
            return self._send(200, json.dumps({"message": default_commit_message(n)}))
        if path == "/usage-history":
            with USAGE_LOCK:
                rows = list(reversed(HISTORY))
            return self._send(200, json.dumps({"history": rows}))
        if path == "/events":
            with EVENTS_LOCK:
                rows = list(reversed(EVENTS))
            return self._send(200, json.dumps({"events": rows}))
        if path == "/self-git":
            do_fetch = (query.get("fetch") or [""])[0] == "1"
            repo = (query.get("repo") or ["self"])[0]
            report = project_git_status(do_fetch=do_fetch) if repo == "project" \
                else self_git_status(do_fetch=do_fetch)
            return self._send(200, json.dumps(report))
        if path.startswith("/files/"):
            n = os.path.basename(path[len("/files/"):])
            if n not in DESKS:
                return self._send(404, b"unknown desk", "text/plain")
            return self._send(200, json.dumps({"files": list_desk_files(n)}))
        if path.startswith("/file/"):
            n = os.path.basename(path[len("/file/"):])
            rel = (query.get("path") or [""])[0]
            if n not in DESKS:
                return self._send(404, b"unknown desk", "text/plain")
            ok, content, truncated = read_desk_file(n, rel)
            if not ok:
                return self._send(404, json.dumps({"error": content}))
            return self._send(200, json.dumps({"path": rel, "content": content, "truncated": truncated}))
        if path.startswith("/reports/"):
            fn = os.path.basename(path[len("/reports/"):])
            try:
                with open(os.path.join(REPORTS, fn), "rb") as f:
                    return self._send(200, f.read(), "text/plain; charset=utf-8")
            except OSError:
                return self._send(404, b"no report", "text/plain")
        return self._send(404, b"nope", "text/plain")

    def do_POST(self):
        global BASE
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
            if desk in CONFLICTS:
                # Mid-cherry-pick on a detached HEAD - a bare reset --hard here
                # leaves the pick half-applied and the desk off its own branch.
                resolve_conflict(desk, "abort")
            git(["reset", "--hard", BASE], cwd=desk_path(desk))
            git(["clean", "-fd"], cwd=desk_path(desk))
            set_(desk, **blank(desk))
            log_event("discard", desk, "work discarded, worktree reset to " + BASE)
            return self._send(200, json.dumps({"ok": True}))

        if path == "/review":
            desk = body.get("agent")
            if desk not in DESKS:
                return self._send(400, json.dumps({"error": "unknown desk"}))
            if not desk_dirty(desk) and not desk_unpushed(desk):
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
                msg = default_commit_message(desk)
            ok, detail = ship_desk(desk, branches, msg)
            if not ok:
                log_event("ship_error", desk, detail, level="error")
                return self._send(409, json.dumps({"error": detail}))
            set_(desk, **blank(desk))
            set_(SHIPPER, reviewed="", verdict="", status="done",
                 detail=detail, output=detail)
            log_event("ship", desk, "pushed to " + ", ".join(branches) + " - " + detail)
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
            if not desk_dirty(desk) and not desk_unpushed(desk):
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

        if path == "/file":
            desk = body.get("agent")
            rel = (body.get("path") or "").strip()
            content = body.get("content")
            if desk not in DESKS:
                return self._send(400, json.dumps({"error": "unknown desk"}))
            if not rel:
                return self._send(400, json.dumps({"error": "no file selected"}))
            if content is None:
                return self._send(400, json.dumps({"error": "no content"}))
            if STATE[desk]["status"] in ("seated", "thinking", "working"):
                return self._send(409, json.dumps({"error": "desk is running - stop it before editing"}))
            ok, err = write_desk_file(desk, rel, content)
            if not ok:
                return self._send(400, json.dumps({"error": err}))
            return self._send(200, json.dumps({"ok": True}))

        if path == "/conflict":
            desk = body.get("agent")
            action = (body.get("action") or "").strip()
            if desk not in DESKS:
                return self._send(400, json.dumps({"error": "unknown desk"}))
            ok, detail = resolve_conflict(desk, action, (body.get("path") or "").strip())
            if not ok:
                return self._send(409, json.dumps({"error": detail}))
            if action in ("ours", "theirs"):
                return self._send(200, json.dumps({"ok": True, "detail": detail}))
            # continue/abort end the ship one way or the other - the shipper's
            # review is spent either way, same as a clean push.
            if desk not in CONFLICTS:
                set_(desk, **blank(desk))
                set_(SHIPPER, reviewed="", verdict="", status="done",
                     detail=detail, output=detail)
            return self._send(200, json.dumps({"ok": True, "detail": detail}))

        if path == "/rename":
            ok, result = rename_agent(body.get("agent"), body.get("name"))
            if not ok:
                return self._send(400, json.dumps({"error": result}))
            return self._send(200, json.dumps({"ok": True, "name": result}))

        if path == "/base":
            branch = (body.get("branch") or "").strip()
            if not branch:
                return self._send(400, json.dumps({"error": "choose a branch"}))
            if branch not in list_all_branches():
                return self._send(400, json.dumps({"error": "no branch named %s on origin" % branch}))
            BASE = branch
            return self._send(200, json.dumps({"ok": True, "base": BASE}))

        if path == "/suggest-module":
            task = (body.get("task") or "").strip()
            if not task:
                return self._send(400, json.dumps({"error": "write a task first"}))
            guess, why = suggest_module(task)
            return self._send(200, json.dumps({"module": guess, "reason": why}))

        if path == "/self-git/push":
            ok, detail = self_commit_and_push((body.get("message") or "").strip())
            log_event("self_push" if ok else "self_push_error", None, detail,
                      level="info" if ok else "error")
            if not ok:
                return self._send(409, json.dumps({"error": detail}))
            return self._send(200, json.dumps({"ok": True, "detail": detail}))

        if path == "/self-git/pull":
            ok, detail = self_pull()
            log_event("self_pull" if ok else "self_pull_error", None, detail,
                      level="info" if ok else "error")
            if not ok:
                return self._send(409, json.dumps({"error": detail}))
            return self._send(200, json.dumps({"ok": True, "detail": detail}))

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
        if desk_dirty(desk) or desk_unpushed(desk):
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

    # list_all_branches() is for the base-branch picker, so main/master ARE allowed there
    assert isinstance(list_all_branches(), list)
    assert set(list_branches()).issubset(set(list_all_branches()))

    # project_git_status() - read-only status of the user's own Project checkout,
    # never fetched here (no network dependency in a test)
    pgs = project_git_status()
    assert isinstance(pgs["branch"], str) and isinstance(pgs["dirty"], list)
    assert pgs["dirty_count"] >= len(pgs["dirty"]), "dirty_count must be the true total, even when the list is capped"
    assert pgs["branches"] == list_all_branches()

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

    # --- file explorer / editor (path safety + read/write round-trip) ---
    assert desk_file_path("desk-1", "../../etc/passwd") is None, "traversal must be blocked"
    assert desk_file_path("desk-1", "sub/../../../etc/passwd") is None
    # os.path.join(root, "/etc/passwd") silently discards root (absolute 2nd
    # arg) - the startswith(root) check downstream must still catch this
    assert desk_file_path("desk-1", "/etc/passwd") is None
    assert desk_file_path("nonexistent-desk", "x.txt") is None
    _wt1 = desk_path("desk-1")
    os.makedirs(_wt1, exist_ok=True)
    _sf = os.path.join(_wt1, "selftest_file.txt")
    try:
        with open(_sf, "w") as f:
            f.write("hello\nworld\n")
        ok, content, truncated = read_desk_file("desk-1", "selftest_file.txt")
        assert ok and content == "hello\nworld\n" and not truncated
        ok, err = write_desk_file("desk-1", "selftest_file.txt", "changed\n")
        assert ok
        ok, content, _ = read_desk_file("desk-1", "selftest_file.txt")
        assert content == "changed\n"
        ok, err = write_desk_file("desk-1", "../outside.txt", "x")
        assert not ok and "invalid" in err
        ok, err, _ = read_desk_file("desk-1", "does-not-exist.txt")
        assert not ok and "not found" in err
        assert isinstance(list_desk_files("desk-1"), list)
    finally:
        if os.path.exists(_sf):
            os.remove(_sf)

    # --- structured diff parsing (pure text - git already computed the diff) ---
    _sample_diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index e69de29..4b825dc 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,3 @@\n"
        " line1\n"
        "-old line\n"
        "+new line\n"
        "+added line\n"
    )
    _parsed = parse_unified_diff(_sample_diff)
    assert len(_parsed) == 1 and _parsed[0]["file"] == "foo.py" and not _parsed[0]["binary"]
    assert len(_parsed[0]["hunks"]) == 1
    _lines = _parsed[0]["hunks"][0]["lines"]
    assert _lines[0] == [" ", "line1"]
    assert _lines[1] == ["-", "old line"]
    assert _lines[2] == ["+", "new line"] and _lines[3] == ["+", "added line"]
    assert isinstance(desk_diff_json("desk-1"), list)
    # a desk with no worktree yet (git ls-files fails with "No such file or
    # directory") must not treat that OSError string as a list of filenames
    assert desk_diff_json("desk-nonexistent-worktree") == []

    # --- descriptive default commit message (git diff --stat, no AI call) ---
    _orig_task, _orig_ttype = STATE["desk-1"]["task"], STATE["desk-1"]["ttype"]
    STATE["desk-1"]["task"], STATE["desk-1"]["ttype"] = "add attendance summary", "feature"
    try:
        msg = default_commit_message("desk-1")
        assert msg.startswith("New feature: add attendance summary") or "add attendance summary" in msg
        assert "file(s) changed" not in msg, "a clean worktree shouldn't claim files changed"
        _sf2 = os.path.join(desk_path("desk-1"), "selftest_commitmsg.txt")
        with open(_sf2, "w") as f:
            f.write("x\n")
        try:
            msg2 = default_commit_message("desk-1")
            assert "1 file(s) changed" in msg2 and "selftest_commitmsg.txt" in msg2
        finally:
            os.remove(_sf2)

        # direct in-browser file edit, never "Assign"-ed -> no task text at
        # all. This exact combo used to leave the subject line as a bare
        # "New feature" with the real info hidden in the hover-only body.
        STATE["desk-1"]["task"] = ""
        _sf3 = os.path.join(desk_path("desk-1"), "selftest_notask.txt")
        with open(_sf3, "w") as f:
            f.write("x\n")
        try:
            msg3 = default_commit_message("desk-1")
            assert "selftest_notask.txt" in msg3.splitlines()[0], \
                "with no task text, the changed file name is the only useful subject-line content"
        finally:
            os.remove(_sf3)
    finally:
        STATE["desk-1"]["task"], STATE["desk-1"]["ttype"] = _orig_task, _orig_ttype

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

    # --- token ledger ---
    _real_usage, _real_usage_file = USAGE, USAGE_FILE
    _real_history, _real_history_file = HISTORY, HISTORY_FILE
    _real_events, _real_events_file = EVENTS, EVENTS_FILE
    globals()["USAGE"] = load_usage()
    globals()["USAGE"].update(today=token_bucket(), total=token_bucket(), models={})
    globals()["USAGE_FILE"] = USAGE_FILE + ".selftest-tmp"
    globals()["HISTORY"] = []
    globals()["HISTORY_FILE"] = HISTORY_FILE + ".selftest-tmp"
    globals()["EVENTS"] = []
    globals()["EVENTS_FILE"] = EVENTS_FILE + ".selftest-tmp"
    try:
        set_("desk-1", tokens={k: 0 for k in TOKEN_KEYS}, task="add attendance summary", ttype="feature")
        # two assistant turns tick the live counter up
        for _ in range(2):
            track_usage({"type": "assistant", "message": {"usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_read_input_tokens": 800, "cache_creation_input_tokens": 200}}},
                "desk-1")
        assert STATE["desk-1"]["tokens"]["out"] == 10, "live counter should accumulate"
        assert USAGE["total"]["runs"] == 0, "assistant events must not touch the ledger"

        track_usage({"type": "result", "subtype": "success", "total_cost_usd": 0.25,
                     "usage": {"input_tokens": 20, "output_tokens": 55,
                               "cache_read_input_tokens": 900,
                               "cache_creation_input_tokens": 100},
                     "modelUsage": {"claude-haiku-4-5-20251001": {
                         "inputTokens": 20, "outputTokens": 55,
                         "cacheReadInputTokens": 900, "cacheCreationInputTokens": 100,
                         "costUSD": 0.25}}}, "desk-1")
        assert STATE["desk-1"]["tokens"]["out"] == 55, \
            "the result total should replace the running count, not add to it"
        assert USAGE["total"]["runs"] == 1 and USAGE["today"]["cost"] == 0.25
        assert USAGE["models"]["claude-haiku-4-5-20251001"]["cache_read"] == 900

        # --- per-run history (the "per task per session" breakdown) ---
        assert len(HISTORY) == 1, "a completed desk run should append one history row"
        row = HISTORY[0]
        assert row["desk"] == "desk-1" and row["task"] == "add attendance summary"
        assert row["ttype"] == "feature" and row["cost"] == 0.25
        assert row["tokens"]["out"] == 55, "history should keep this run's own totals, not the ledger's"

        # a helper call with no desk still lands in the ledger - these aren't free
        track_usage({"type": "result", "subtype": "success", "total_cost_usd": 0.05,
                     "usage": {"input_tokens": 5, "output_tokens": 5}})
        assert USAGE["total"]["runs"] == 2, "deskless helper calls must be counted"
        assert len(HISTORY) == 1, "a deskless helper call has no task to attribute - it stays out of history"

        d = derive(USAGE["total"])
        assert d["total"] == 20 + 900 + 100 + 55 + 5 + 5
        assert d["cache_hit"] == round(100.0 * 900 / (25 + 900 + 100), 1)
        assert derive(token_bucket())["cache_hit"] == 0.0, "empty ledger must not divide by zero"

        for _ in range(HISTORY_MAX + 5):
            track_usage({"type": "result", "subtype": "success", "total_cost_usd": 0.01,
                         "usage": {"input_tokens": 1, "output_tokens": 1}}, "desk-1")
        assert len(HISTORY) == HISTORY_MAX, "history must stay capped, not grow without bound"

        # --- activity log / debug log file ---
        log_event("task_done", "desk-1", "finished ok")
        log_event("ship_error", "desk-2", "push rejected", level="error")
        assert len(EVENTS) == 2
        assert EVENTS[0]["kind"] == "task_done" and EVENTS[0]["desk"] == "desk-1"
        assert EVENTS[1]["level"] == "error" and EVENTS[1]["message"] == "push rejected"
        for _ in range(EVENTS_MAX + 5):
            log_event("task_done", "desk-1", "x")
        assert len(EVENTS) == EVENTS_MAX, "events must stay capped too"
        assert os.path.exists(LOG_FILE), "log_event must actually write to the log file"
        with open(LOG_FILE) as f:
            assert "[task_done] desk-1: finished ok" in f.read(), \
                "the log file line must be traceable back to the event that wrote it"
    finally:
        if os.path.exists(globals()["HISTORY_FILE"]):
            os.remove(globals()["HISTORY_FILE"])
        if os.path.exists(globals()["EVENTS_FILE"]):
            os.remove(globals()["EVENTS_FILE"])
        globals()["USAGE"], globals()["USAGE_FILE"] = _real_usage, _real_usage_file
        globals()["HISTORY"], globals()["HISTORY_FILE"] = _real_history, _real_history_file
        globals()["EVENTS"], globals()["EVENTS_FILE"] = _real_events, _real_events_file
        set_("desk-1", **blank("desk-1"))
        try:
            os.remove(_real_usage_file + ".selftest-tmp")
        except OSError:
            pass

    # --- conflict resolver, end to end against a throwaway repo ---
    # This one is worth the setup cost: a resolver that pushes a file with
    # conflict markers still in it is worse than no resolver at all.
    import tempfile
    tmp = tempfile.mkdtemp(prefix="fleet-selftest-")
    _real_wt, _real_base = WT, BASE
    globals()["WT"] = os.path.join(tmp, "wt")
    globals()["BASE"] = "stage"        # the throwaway repo's only branch
    _real_cfile = CONFLICTS_FILE
    globals()["CONFLICTS_FILE"] = os.path.join(tmp, "conflicts.json")

    os.makedirs(WT)
    ident = ["-c", "user.name=selftest", "-c", "user.email=selftest@local"]
    try:
        origin = os.path.join(tmp, "origin.git")
        assert git(["init", "--bare", "-b", "stage", origin])[0] == 0

        seed = os.path.join(tmp, "seed")
        assert git(["clone", origin, seed])[0] == 0
        with open(os.path.join(seed, "f.txt"), "w") as f:
            f.write("base\n")
        git(["add", "-A"], cwd=seed)
        git(ident + ["commit", "-m", "base"], cwd=seed)
        git(["push", "origin", "HEAD:stage"], cwd=seed)

        # the desk branches off that base and edits the line
        desk = os.path.join(WT, "desk-1")
        assert git(["clone", "-b", "stage", origin, desk])[0] == 0
        git(["checkout", "-b", "fleet/desk-1"], cwd=desk)
        with open(os.path.join(desk, "f.txt"), "w") as f:
            f.write("desk version\n")
        git(["add", "-A"], cwd=desk)
        git(ident + ["commit", "-m", "desk change"], cwd=desk)
        sha = git(["rev-parse", "HEAD"], cwd=desk)[1].strip()

        # meanwhile stage moves on, touching the same line -> guaranteed conflict
        with open(os.path.join(seed, "f.txt"), "w") as f:
            f.write("stage version\n")
        git(["add", "-A"], cwd=seed)
        git(ident + ["commit", "-m", "stage change"], cwd=seed)
        git(["push", "origin", "HEAD:stage"], cwd=seed)

        ok, msg = push_branches("desk-1", sha, ["stage"], [])
        assert not ok and "conflict" in msg, msg
        assert "desk-1" in CONFLICTS, "the conflict must be left paused, not aborted"
        assert conflict_files("desk-1") == ["f.txt"], conflict_files("desk-1")
        assert "<<<<<<<" in open(os.path.join(desk, "f.txt")).read(), \
            "the working tree must keep the markers so they can be edited"

        ok, msg = resolve_conflict("desk-1", "continue")
        assert not ok and "markers are still in" in msg, \
            "continuing with markers left in the file must be refused"
        assert has_markers("desk-1", "f.txt")

        # A paused pick must survive a restart - git keeps the worktree
        # mid-cherry-pick, so an in-memory-only dict would strand the work.
        assert cherry_pick_paused("desk-1")
        reloaded = load_conflicts()
        assert reloaded.get("desk-1", {}).get("branch") == "stage", \
            "the paused conflict must come back after a restart"

        ok, msg = resolve_conflict("desk-1", "theirs", "f.txt")
        assert ok, msg
        assert open(os.path.join(desk, "f.txt")).read() == "desk version\n"
        assert conflict_files("desk-1") == []
        assert not has_markers("desk-1", "f.txt")

        # Now the path the UI actually takes: the file is edited in the
        # browser and written straight to disk - nothing runs `git add`, so
        # the index stays unmerged and only a marker scan can tell whether
        # the user is really done. (This is "Accept both" in the hunk view.)
        with open(os.path.join(desk, "f.txt"), "w") as f:
            f.write("desk version\nstage version\n")
        ok, msg = resolve_conflict("desk-1", "continue")
        assert ok, msg
        assert "desk-1" not in CONFLICTS
        landed = git(["show", "stage:f.txt"], cwd=origin)[1]
        assert landed == "desk version\nstage version\n", repr(landed)
        assert git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=desk)[1].strip() == "fleet/desk-1", \
            "the desk must be back on its own branch afterwards"
        assert not desk_unpushed("desk-1"), \
            "after a successful ship the desk must look free - the cherry-pick gets a new " \
            "sha, so leaving the original on fleet/<desk> would brick the desk forever"

        assert resolve_conflict("desk-1", "continue")[0] is False, \
            "resolving twice must not do anything a second time"
        assert not cherry_pick_paused("desk-1")
        assert load_conflicts() == {}, \
            "a finished pick must not be reloaded as a stale conflict"
    finally:
        globals()["WT"], globals()["BASE"] = _real_wt, _real_base
        globals()["CONFLICTS_FILE"] = _real_cfile
        CONFLICTS.pop("desk-1", None)
        shutil.rmtree(tmp, ignore_errors=True)

    # --- self-git: an isolated temp repo + temp bare "origin" stand in for
    # HERE, so this exercises real git add/commit/push/pull without ever
    # touching the actual dashboard repo or the network. ---
    tmp2 = tempfile.mkdtemp(prefix="fleet-selftest-selfgit-")
    ident = ["-c", "user.name=selftest", "-c", "user.email=selftest@local"]
    try:
        origin2 = os.path.join(tmp2, "origin.git")
        assert git(["init", "--bare", "-b", "main", origin2])[0] == 0
        seed2 = os.path.join(tmp2, "seed")
        assert git(["clone", origin2, seed2])[0] == 0
        with open(os.path.join(seed2, "fleet.py"), "w") as f:
            f.write("# v1\n")
        git(["add", "-A"], cwd=seed2)
        git(ident + ["commit", "-m", "init"], cwd=seed2)
        git(["push", "origin", "HEAD:main"], cwd=seed2)
        git(["checkout", "-b", "dev"], cwd=seed2)
        git(["push", "-u", "origin", "dev"], cwd=seed2)

        _real_here = HERE
        globals()["HERE"] = seed2
        try:
            st = self_git_status()
            assert st["branch"] == "dev" and st["ahead"] == 0 and st["behind"] == 0
            assert st["dirty"] == [] and st["last_commit"]["subject"] == "init"

            ok, detail = self_commit_and_push("")
            assert not ok and "up to date" in detail, "a clean, unpushed-nothing repo has nothing to push"

            with open(os.path.join(seed2, "fleet.py"), "w") as f:
                f.write("# v2 - changed\n")
            assert self_git_status()["dirty"] == ["fleet.py"]
            assert self_default_commit_message() == "Update: fleet.py"

            ok, detail = self_commit_and_push("")
            assert ok and "pushed" in detail, detail
            assert self_git_status()["dirty"] == [] and self_git_status()["ahead"] == 0
            landed = git(["log", "-1", "--format=%s", "dev"], cwd=origin2)[1].strip()
            assert landed == "Update: fleet.py", "the auto-generated message must be what actually landed"

            # pushing straight to main must work here - unlike desk-ship, this
            # repo has no stage/saad-dev gating, main *is* the real workflow
            git(["checkout", "main"], cwd=seed2)
            with open(os.path.join(seed2, "fleet.py"), "w") as f:
                f.write("# v3\n")
            ok, detail = self_commit_and_push("")
            assert ok and "pushed to origin/main" in detail, detail
            assert git(["log", "-1", "--format=%s", "main"], cwd=origin2)[1].strip() == "Update: fleet.py"
            git(["checkout", "dev"], cwd=seed2)

            # pull: refuses on dirty, ff-only otherwise
            with open(os.path.join(seed2, "fleet.py"), "w") as f:
                f.write("# dirty\n")
            ok, detail = self_pull()
            assert not ok and "commit or discard" in detail
            git(["checkout", "--", "fleet.py"], cwd=seed2)

            # advance origin/dev from a second clone, then pull should fast-forward
            seed3 = os.path.join(tmp2, "seed3")
            assert git(["clone", "-b", "dev", origin2, seed3])[0] == 0
            with open(os.path.join(seed3, "index.html"), "w") as f:
                f.write("<!-- v2 -->\n")
            git(["add", "-A"], cwd=seed3)
            git(ident + ["commit", "-m", "from teammate"], cwd=seed3)
            git(["push", "origin", "dev"], cwd=seed3)

            assert self_git_status(do_fetch=True)["behind"] == 1
            ok, detail = self_pull()
            assert ok and "restart" in detail
            assert os.path.exists(os.path.join(seed2, "index.html")), \
                "the fast-forward must actually update the working tree"
            assert self_git_status()["behind"] == 0
        finally:
            globals()["HERE"] = _real_here
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

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
    log_event("server_start", None, "listening on 127.0.0.1:%d, project=%s, base=%s" % (PORT, PROJECT, BASE))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
