#!/usr/bin/env python3
"""
Antigravity (AGY) Manager Web Service for OMV NAS
Provides Web UI to:
- Interactive automated Google OAuth login & manual token import
- Switch / Delete accounts
- Start / Stop / Restart & View status of remote-control daemon
- Toggle Full Permission (--dangerously-skip-permissions / always-proceed)
- Real-time live logs
- Usage & Quota monitoring (Gemini & Claude/GPT models)
"""

import os
import sys
import json
import time
import base64
import shutil
import re
import subprocess
import threading
import pty
import select
import secrets
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

def _load_env_file():
    for env_path in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        "/home/chungnh/AI Workspace/projects/antigravity-manager/.env",
        "/docker-files/agy-manager/agy-manager.env"
    ]:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            if k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass

_load_env_file()

AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "true").lower() in ("true", "1", "yes")
ADMIN_USER = os.environ.get("ADMIN_USER") or os.environ.get("AUTH_USER") or "chungnh"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or os.environ.get("AUTH_PASSWORD") or "As123456"

WEB_SESSIONS = {}
WEB_SESSIONS_LOCK = threading.Lock()

def create_web_session(user):
    token = secrets.token_hex(24)
    with WEB_SESSIONS_LOCK:
        WEB_SESSIONS[token] = {
            "user": user,
            "expires": time.time() + 86400 * 30  # 30 days
        }
    return token

def validate_web_session(token):
    if not token:
        return None
    with WEB_SESSIONS_LOCK:
        sess = WEB_SESSIONS.get(token)
        if sess:
            if sess["expires"] > time.time():
                return sess["user"]
            else:
                del WEB_SESSIONS[token]
    return None

def delete_web_session(token):
    if not token:
        return
    with WEB_SESSIONS_LOCK:
        WEB_SESSIONS.pop(token, None)

PORT = int(os.environ.get("PORT", "8585"))

def _detect_default_user():
    user = os.environ.get("HOST_USER") or os.environ.get("USER")
    if user and user != "root":
        return user
    if os.path.exists("/home"):
        try:
            users = [d for d in os.listdir("/home") if os.path.isdir(os.path.join("/home", d)) and not d.startswith(".")]
            if len(users) == 1:
                return users[0]
            elif "chungnh" in users:
                return "chungnh"
        except Exception:
            pass
    return user or "root"

HOST_USER = os.environ.get("HOST_USER") or _detect_default_user()
USER_HOME = os.environ.get("USER_HOME") or (f"/home/{HOST_USER}" if HOST_USER != "root" else "/root")
GEMINI_DIR = os.path.join(USER_HOME, ".gemini")
AGY_CLI_DIR = os.path.join(GEMINI_DIR, "antigravity-cli")
PROFILES_DIR = os.path.join(GEMINI_DIR, "profiles")
PROFILES_JSON = os.path.join(PROFILES_DIR, "profiles.json")
AUTH_FILE = os.path.join(AGY_CLI_DIR, "web_auth.json")

def hash_password(password, salt=None):
    if not salt:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000).hex()
    return salt, pwd_hash

def verify_password(password, salt, expected_hash):
    if not salt or not expected_hash:
        return False
    _, calculated_hash = hash_password(password, salt)
    return hmac.compare_digest(calculated_hash, expected_hash)

def get_auth_credentials():
    """Load credentials from persistent web_auth.json or fallback to env."""
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r") as f:
                data = json.load(f)
                return data.get("username", ADMIN_USER), data.get("salt"), data.get("password_hash")
        except Exception:
            pass
    # Initialize from env
    salt, pwd_hash = hash_password(ADMIN_PASSWORD)
    try:
        os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
        with open(AUTH_FILE, "w") as f:
            json.dump({
                "username": ADMIN_USER,
                "salt": salt,
                "password_hash": pwd_hash,
                "updated_at": int(time.time())
            }, f, indent=2)
    except Exception:
        pass
    return ADMIN_USER, salt, pwd_hash

def update_auth_password(new_password):
    """Update password with a fresh salt and hash in web_auth.json."""
    user, _, _ = get_auth_credentials()
    salt, pwd_hash = hash_password(new_password)
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    with open(AUTH_FILE, "w") as f:
        json.dump({
            "username": user,
            "salt": salt,
            "password_hash": pwd_hash,
            "updated_at": int(time.time())
        }, f, indent=2)
    return True

FAILED_ATTEMPTS = {}
FAILED_ATTEMPTS_LOCK = threading.Lock()

def check_rate_limit(ip):
    now = time.time()
    with FAILED_ATTEMPTS_LOCK:
        record = FAILED_ATTEMPTS.get(ip)
        if record:
            if record["blocked_until"] > now:
                remaining = int(record["blocked_until"] - now)
                return False, f"Tài khoản bị tạm khóa do nhập sai nhiều lần ({remaining}s còn lại)"
            if record["blocked_until"] <= now and record["count"] >= 5:
                del FAILED_ATTEMPTS[ip]
    return True, None

def record_failed_attempt(ip):
    now = time.time()
    with FAILED_ATTEMPTS_LOCK:
        record = FAILED_ATTEMPTS.setdefault(ip, {"count": 0, "blocked_until": 0})
        record["count"] += 1
        if record["count"] >= 5:
            record["blocked_until"] = now + 900  # Lock 15 minutes

def clear_failed_attempts(ip):
    with FAILED_ATTEMPTS_LOCK:
        FAILED_ATTEMPTS.pop(ip, None)

def resolve_agy_bin():
    env_bin = (os.environ.get("AGY_BIN") or "").strip()
    if env_bin:
        return env_bin
    user_local_bin = os.path.join(USER_HOME, ".local/bin/agy")
    if os.path.exists(user_local_bin):
        return user_local_bin
    which_bin = shutil.which("agy")
    if which_bin:
        return which_bin
    return user_local_bin

AGY_BIN = resolve_agy_bin()
DAEMON_SVC = os.environ.get("SYSTEMD_SERVICE", "antigravity-cli-daemon.service")
DAEMON_SVC_PATH = os.path.join(USER_HOME, f".config/systemd/user/{DAEMON_SVC}")
SETTINGS_PATH = os.path.join(AGY_CLI_DIR, "settings.json")
IS_DOCKER = os.path.exists("/.dockerenv") or os.environ.get("IS_DOCKER", "").lower() in ("true", "1", "yes")

def get_instance_name():
    if os.environ.get("INSTANCE_NAME"):
        return os.environ.get("INSTANCE_NAME")
    try:
        code, name, _ = run_host_cmd("hostname")
        if code == 0 and name.strip():
            return name.strip()
    except Exception:
        pass
    return os.uname().nodename

# In-memory storage for interactive login sessions
LOGIN_SESSIONS = {}

def run_host_cmd(cmd_str, timeout=15):
    """Run command on the host (either natively or via nsenter if inside container)."""
    if IS_DOCKER:
        full_cmd = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "su", "-", HOST_USER, "-c", cmd_str]
    else:
        full_cmd = ["su", "-", HOST_USER, "-c", cmd_str] if os.geteuid() == 0 and HOST_USER != "root" else ["bash", "-c", cmd_str]
    try:
        res = subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return res.returncode, res.stdout.strip(), res.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout expired"
    except Exception as e:
        return -1, "", str(e)

def get_permission_mode():
    """Check permission mode in systemd service and settings.json."""
    has_flag = False
    tool_perm = "always-proceed"

    if os.path.exists(DAEMON_SVC_PATH):
        try:
            with open(DAEMON_SVC_PATH, "r") as f:
                has_flag = "--dangerously-skip-permissions" in f.read()
        except Exception:
            pass

    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                st = json.load(f)
            tool_perm = st.get("toolPermission", "always-proceed")
        except Exception:
            pass

    current_mode = tool_perm
    if has_flag and current_mode != "always-proceed":
        current_mode = "always-proceed"

    return {
        "current_mode": current_mode,
        "has_flag": has_flag,
        "tool_permission": tool_perm
    }

def set_permission_mode(mode: str):
    """Update daemon ExecStart flag and settings.json toolPermission for the selected mode."""
    valid_modes = ["always-proceed", "agent-decides", "request-review", "proceed-in-sandbox"]
    if mode not in valid_modes:
        mode = "always-proceed"

    # 1. Update systemd service file
    if os.path.exists(DAEMON_SVC_PATH):
        try:
            with open(DAEMON_SVC_PATH, "r") as f:
                content = f.read()
            
            if mode == "always-proceed":
                content = re.sub(
                    r"ExecStart=.*agy.*remote-control serve",
                    f"ExecStart={AGY_BIN} --dangerously-skip-permissions remote-control serve",
                    content
                )
            else:
                content = re.sub(
                    r"ExecStart=.*agy.*remote-control serve",
                    f"ExecStart={AGY_BIN} remote-control serve",
                    content
                )
                
            with open(DAEMON_SVC_PATH, "w") as f:
                f.write(content)
        except Exception as e:
            return False, f"Failed to update service file: {e}"

    # 2. Update settings.json
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r") as f:
                st = json.load(f)
            st["toolPermission"] = mode
            with open(SETTINGS_PATH, "w") as f:
                json.dump(st, f, indent=2)
        except Exception as e:
            return False, f"Failed to update settings.json: {e}"

    run_host_cmd("systemctl --user daemon-reload")
    _, out, _ = run_host_cmd(f"systemctl --user is-active {DAEMON_SVC}")
    if out.strip() == "active":
        run_host_cmd(f"systemctl --user restart {DAEMON_SVC}")

    mode_labels = {
        "always-proceed": "⚡ always-proceed (Full Auto)",
        "agent-decides": "🤖 agent-decides (AI Quyết Định)",
        "request-review": "🛡️ request-review (Hỏi Xác Nhận)",
        "proceed-in-sandbox": "📦 proceed-in-sandbox (Sandbox Cô Lập)"
    }
    msg = f"Đã chuyển sang mode: {mode_labels.get(mode, mode)}"
    return True, msg

def get_daemon_status():
    """Check status of the systemd daemon service."""
    code, out, _ = run_host_cmd(f"systemctl --user is-active {DAEMON_SVC}")
    is_active = (out == "active")
    
    details = {
        "active": is_active,
        "status": out if out else "inactive",
        "pid": "-",
        "memory": "-",
        "cpu": "-",
        "active_since": "-",
        "instance_name": get_instance_name()
    }
    
    if is_active:
        _, status_out, _ = run_host_cmd(f"systemctl --user status {DAEMON_SVC} --no-pager")
        for line in status_out.splitlines():
            line_str = line.strip()
            if "Main PID:" in line_str:
                m = re.search(r"Main PID:\s+(\d+)", line_str)
                if m: details["pid"] = m.group(1)
            elif "Memory:" in line_str:
                m = re.search(r"Memory:\s+([^\n]+)", line_str)
                if m: details["memory"] = m.group(1).strip()
            elif "CPU:" in line_str:
                m = re.search(r"CPU:\s+([^\n]+)", line_str)
                if m: details["cpu"] = m.group(1).strip()
            elif "Active: active (running) since" in line_str:
                m = re.search(r"since\s+([^;]+)", line_str)
                if m: details["active_since"] = m.group(1).strip()
    return details

def get_daemon_logs(lines=60):
    """Fetch journalctl logs for the daemon."""
    code, out, _ = run_host_cmd(f"journalctl --user -u {DAEMON_SVC} -n {lines} --no-pager")
    return out

def get_active_account():
    """Extract email and info of current active account."""
    token_file = os.path.join(AGY_CLI_DIR, "antigravity-oauth-token")
    email = "Unknown"
    expiry = "-"
    if os.path.exists(token_file):
        try:
            with open(token_file) as f:
                tok = json.load(f)
            id_token = tok.get("id_token")
            if id_token:
                parts = id_token.split(".")
                if len(parts) >= 2:
                    payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                    data = json.loads(base64.urlsafe_b64decode(payload))
                    email = data.get("email", "Unknown")
                    exp = data.get("exp")
                    if exp:
                        expiry = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))
        except Exception as e:
            email = f"Error reading: {e}"
    return {"email": email, "expiry": expiry}

def get_profiles_data():
    """Read saved profiles list."""
    if not os.path.exists(PROFILES_JSON):
        os.makedirs(os.path.join(PROFILES_DIR, "default"), exist_ok=True)
        acc = get_active_account()
        data = {
            "active_profile": "default",
            "profiles": [
                {
                    "id": "default",
                    "name": f"Default ({acc['email']})",
                    "email": acc['email'],
                    "created_at": time.strftime('%Y-%m-%dT%H:%M:%SZ')
                }
            ]
        }
        with open(PROFILES_JSON, "w") as f:
            json.dump(data, f, indent=2)
        return data
    try:
        with open(PROFILES_JSON) as f:
            return json.load(f)
    except Exception:
        return {"active_profile": "", "profiles": []}

def save_current_as_profile(profile_id, profile_name):
    """Snapshot current credentials into a profile."""
    prof_dir = os.path.join(PROFILES_DIR, profile_id)
    os.makedirs(prof_dir, exist_ok=True)
    
    files = [
        (os.path.join(AGY_CLI_DIR, "antigravity-oauth-token"), os.path.join(prof_dir, "antigravity-oauth-token")),
        (os.path.join(GEMINI_DIR, "oauth_creds.json"), os.path.join(prof_dir, "oauth_creds.json")),
        (os.path.join(GEMINI_DIR, "google_accounts.json"), os.path.join(prof_dir, "google_accounts.json")),
    ]
    for src, dst in files:
        if os.path.exists(src):
            shutil.copy2(src, dst)
            
    acc = get_active_account()
    data = get_profiles_data()
    exists = False
    for p in data.get("profiles", []):
        if p["id"] == profile_id:
            p["name"] = profile_name
            p["email"] = acc["email"]
            exists = True
            break
    if not exists:
        data.setdefault("profiles", []).append({
            "id": profile_id,
            "name": profile_name,
            "email": acc["email"],
            "created_at": time.strftime('%Y-%m-%dT%H:%M:%SZ')
        })
    with open(PROFILES_JSON, "w") as f:
        json.dump(data, f, indent=2)
    return True

def switch_profile(profile_id):
    """Switch active credentials to chosen profile and restart daemon."""
    prof_dir = os.path.join(PROFILES_DIR, profile_id)
    if not os.path.exists(prof_dir):
        return False, "Profile directory not found"
        
    files = [
        (os.path.join(prof_dir, "antigravity-oauth-token"), os.path.join(AGY_CLI_DIR, "antigravity-oauth-token")),
        (os.path.join(prof_dir, "oauth_creds.json"), os.path.join(GEMINI_DIR, "oauth_creds.json")),
        (os.path.join(prof_dir, "google_accounts.json"), os.path.join(GEMINI_DIR, "google_accounts.json")),
    ]
    for src, dst in files:
        if os.path.exists(src):
            shutil.copy2(src, dst)
            
    data = get_profiles_data()
    data["active_profile"] = profile_id
    with open(PROFILES_JSON, "w") as f:
        json.dump(data, f, indent=2)
        
    run_host_cmd(f"systemctl --user restart {DAEMON_SVC}")
    return True, "Switched successfully"

def delete_profile(profile_id):
    """Delete a saved profile (cannot delete active profile)."""
    data = get_profiles_data()
    if data.get("active_profile") == profile_id:
        return False, "Không thể xóa profile đang hoạt động (Active)"
    
    data["profiles"] = [p for p in data.get("profiles", []) if p["id"] != profile_id]
    with open(PROFILES_JSON, "w") as f:
        json.dump(data, f, indent=2)
        
    prof_dir = os.path.join(PROFILES_DIR, profile_id)
    if os.path.exists(prof_dir):
        shutil.rmtree(prof_dir, ignore_errors=True)
    return True, "Đã xóa profile thành công"

def add_new_account(profile_name, token_json_str, activate_now=False):
    """Add new account from token JSON string."""
    try:
        tok_data = json.loads(token_json_str)
    except Exception as e:
        return False, f"Invalid JSON format: {e}"
        
    email = "Unknown"
    id_token = tok_data.get("id_token")
    if id_token:
        try:
            parts = id_token.split(".")
            if len(parts) >= 2:
                payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                email = json.loads(base64.urlsafe_b64decode(payload)).get("email", "Unknown")
        except Exception:
            pass
            
    profile_id = re.sub(r"[^a-zA-Z0-9_-]", "_", profile_name.lower()) + "_" + str(int(time.time()))[-4:]
    prof_dir = os.path.join(PROFILES_DIR, profile_id)
    os.makedirs(prof_dir, exist_ok=True)
    
    with open(os.path.join(prof_dir, "antigravity-oauth-token"), "w") as f:
        json.dump(tok_data, f, indent=2)
        
    with open(os.path.join(prof_dir, "google_accounts.json"), "w") as f:
        json.dump({"active": email, "old": []}, f, indent=2)
        
    data = get_profiles_data()
    data.setdefault("profiles", []).append({
        "id": profile_id,
        "name": profile_name,
        "email": email,
        "created_at": time.strftime('%Y-%m-%dT%H:%M:%SZ')
    })
    with open(PROFILES_JSON, "w") as f:
        json.dump(data, f, indent=2)
        
    if activate_now:
        switch_profile(profile_id)
        
    return True, f"Account {email} added successfully"

# --- Automated OAuth Flow Helpers ---
def start_oauth_session(profile_name, activate_now=True):
    """Spawn an independent agy process in an isolated temp HOME directory using a pseudo-terminal (PTY) to capture Google OAuth URL without touching the main daemon."""
    session_id = f"sess_{int(time.time())}"
    tmp_home = os.path.join(GEMINI_DIR, "tmp", f"login_{session_id}")
    
    # Ensure tmp_home is created with proper ownership on host
    run_host_cmd(f"mkdir -p {tmp_home} && chown -R {HOST_USER} {tmp_home} && chmod 775 {tmp_home}")
    
    # Run host command that starts an independent agy process
    if IS_DOCKER:
        cmd = ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "su", "-", HOST_USER, "-c", f"HOME={tmp_home} {AGY_BIN} -p 'ping'"]
    else:
        cmd = ["su", "-", HOST_USER, "-c", f"HOME={tmp_home} {AGY_BIN} -p 'ping'"] if os.geteuid() == 0 and HOST_USER != "root" else ["bash", "-c", f"HOME={tmp_home} {AGY_BIN} -p 'ping'"]

    master, slave = pty.openpty()
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)  # close slave fd in parent

    session = {
        "id": session_id,
        "proc": proc,
        "master_fd": master,
        "tmp_home": tmp_home,
        "profile_name": profile_name,
        "activate_now": activate_now,
        "auth_url": None,
        "status": "starting",
        "email": None,
        "error": None,
        "started_at": time.time(),
        "logs": []
    }
    LOGIN_SESSIONS[session_id] = session

    # Background thread to monitor PTY master output and token generation
    def _reader_thread():
        buffer = ""
        try:
            while proc.poll() is None:
                r, _, _ = select.select([master], [], [], 0.5)
                if r:
                    try:
                        chunk = os.read(master, 2048).decode('utf-8', errors='replace')
                        if not chunk:
                            break
                        buffer += chunk
                        session["logs"].append(chunk)
                        m = re.search(r"(https://accounts\.google\.com/o/oauth2/auth[^\s\x1b\r\n]+)", buffer)
                        if m and not session["auth_url"]:
                            session["auth_url"] = m.group(1)
                            session["status"] = "waiting_user"
                    except OSError:
                        break
        except Exception as e:
            session["error"] = str(e)
            session["status"] = "failed"
            
    t = threading.Thread(target=_reader_thread, daemon=True)
    t.start()

    # Wait up to 8s for URL extraction
    start_t = time.time()
    while time.time() - start_t < 8:
        if session["auth_url"]:
            break
        if session.get("error") or proc.poll() is not None:
            break
        time.sleep(0.2)
        
    last_err = session.get("error")
    if not session["auth_url"] and session.get("logs"):
        last_err = "".join(session["logs"])[-300:].strip()
        
    return session_id, session["auth_url"], last_err

def check_oauth_session(session_id):
    """Check if token was generated in tmp_home and finalize profile."""
    session = LOGIN_SESSIONS.get(session_id)
    if not session:
        return {"status": "not_found"}
        
    tmp_home = session["tmp_home"]
    token_file = os.path.join(tmp_home, ".gemini/antigravity-cli/antigravity-oauth-token")
    
    if os.path.exists(token_file):
        try:
            with open(token_file) as f:
                tok_data = json.load(f)
            email = "Unknown"
            id_token = tok_data.get("id_token")
            if id_token:
                parts = id_token.split(".")
                if len(parts) >= 2:
                    payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                    email = json.loads(base64.urlsafe_b64decode(payload)).get("email", "Unknown")

            profile_name = session["profile_name"] or f"Google ({email})"
            profile_id = re.sub(r"[^a-zA-Z0-9_-]", "_", profile_name.lower()) + "_" + str(int(time.time()))[-4:]
            prof_dir = os.path.join(PROFILES_DIR, profile_id)
            os.makedirs(prof_dir, exist_ok=True)
            
            shutil.copy2(token_file, os.path.join(prof_dir, "antigravity-oauth-token"))
            # Copy other cred files if generated
            src_creds = os.path.join(tmp_home, ".gemini/oauth_creds.json")
            if os.path.exists(src_creds):
                shutil.copy2(src_creds, os.path.join(prof_dir, "oauth_creds.json"))
            with open(os.path.join(prof_dir, "google_accounts.json"), "w") as f:
                json.dump({"active": email, "old": []}, f, indent=2)

            data = get_profiles_data()
            data.setdefault("profiles", []).append({
                "id": profile_id,
                "name": profile_name,
                "email": email,
                "created_at": time.strftime('%Y-%m-%dT%H:%M:%SZ')
            })
            with open(PROFILES_JSON, "w") as f:
                json.dump(data, f, indent=2)

            # ONLY switch and restart daemon if activate_now was explicitly requested
            if session["activate_now"]:
                switch_profile(profile_id)

            # Cleanup
            try:
                session["proc"].kill()
            except Exception:
                pass
            if session.get("master_fd"):
                try:
                    os.close(session["master_fd"])
                except Exception:
                    pass
            run_host_cmd(f"rm -rf {tmp_home}")
            session["status"] = "completed"
            session["email"] = email
            return {"status": "completed", "email": email, "profile_id": profile_id, "activated": session["activate_now"]}
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    if session["proc"].poll() is not None and not os.path.exists(token_file):
        err_msg = "".join(session.get("logs", []))[-300:].strip() or "Login process exited prematurely"
        return {"status": "failed", "error": err_msg}

    if time.time() - session["started_at"] > 180:
        try:
            session["proc"].kill()
        except Exception:
            pass
        if session.get("master_fd"):
            try:
                os.close(session["master_fd"])
            except Exception:
                pass
        run_host_cmd(f"rm -rf {tmp_home}")
        return {"status": "timeout", "error": "Login session timed out (3m)"}

    return {"status": "waiting_user", "auth_url": session["auth_url"]}

def submit_oauth_code(session_id, code_str):
    """Submit manual authorization code to the waiting agy PTY stdin."""
    session = LOGIN_SESSIONS.get(session_id)
    if not session or not session.get("master_fd"):
        return False, "Session not found or expired"
    try:
        os.write(session["master_fd"], (code_str.strip() + "\n").encode())
        return True, "Code submitted, waiting for completion..."
    except Exception as e:
        return False, str(e)

def cancel_oauth_session(session_id):
    """Cancel an active login session."""
    session = LOGIN_SESSIONS.get(session_id)
    if session:
        try:
            session["proc"].kill()
        except Exception:
            pass
        if session.get("master_fd"):
            try:
                os.close(session["master_fd"])
            except Exception:
                pass
        run_host_cmd(f"rm -rf {session['tmp_home']}")
        del LOGIN_SESSIONS[session_id]
    return True

def get_agy_usage():
    """Query agy -p '/usage' and parse remaining quotas."""
    code, out, err = run_host_cmd(f"{AGY_BIN} -p '/usage'", timeout=20)
    if code != 0 or not out:
        return {"error": err or "Failed to query agy usage", "quotas": []}
        
    quotas = []
    lines = out.strip().splitlines()
    for line in lines:
        parts = [p.strip() for p in re.split(r"\t+|\s{2,}", line.strip()) if p.strip()]
        if len(parts) >= 4 and "%" in parts[2]:
            try:
                pct = int(parts[2].replace("%", "").strip())
                quotas.append({
                    "model_group": parts[0],
                    "limit_type": parts[1],
                    "percentage": pct,
                    "reset_time": parts[3]
                })
            except Exception:
                pass
    return {"raw": out, "quotas": quotas, "queried_at": time.strftime('%Y-%m-%d %H:%M:%S')}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="vi" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Antigravity (AGY) Manager - OMV NAS</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #38bdf8;
      --primary-hover: #0284c7;
      --success: #22c55e;
      --warning: #f59e0b;
      --danger: #ef4444;
      --terminal-bg: #090d16;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body { background: var(--bg); color: var(--text); min-height: 100vh; padding: 1.5rem; }
    .container { max-width: 1200px; margin: 0 auto; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 2rem; border-bottom: 1px solid var(--card-border); padding-bottom: 1rem; }
    .logo { display: flex; align-items: center; gap: 0.75rem; }
    .logo-badge { background: linear-gradient(135deg, #0284c7, #38bdf8); color: white; font-weight: bold; padding: 0.35rem 0.75rem; border-radius: 8px; font-size: 1.1rem; }
    .logo h1 { font-size: 1.5rem; font-weight: 700; }
    .badge { display: inline-flex; align-items: center; gap: 0.35rem; padding: 0.25rem 0.6rem; border-radius: 9999px; font-size: 0.8rem; font-weight: 600; }
    .badge-success { background: rgba(34, 197, 94, 0.2); color: var(--success); border: 1px solid rgba(34, 197, 94, 0.4); }
    .badge-danger { background: rgba(239, 68, 68, 0.2); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.4); }
    .badge-warning { background: rgba(245, 158, 11, 0.2); color: var(--warning); border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge-info { background: rgba(56, 189, 248, 0.2); color: var(--primary); border: 1px solid rgba(56, 189, 248, 0.4); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); gap: 1.5rem; margin-bottom: 1.5rem; }
    .card { background: var(--card); border: 1px solid var(--card-border); border-radius: 12px; padding: 1.25rem; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2); }
    .card-title { font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; display: flex; justify-content: space-between; align-items: center; }
    .btn { background: var(--primary); color: #0f172a; border: none; padding: 0.5rem 1rem; border-radius: 6px; font-weight: 600; cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.9rem; text-decoration: none; }
    .btn:hover { background: var(--primary-hover); color: #fff; }
    .btn-secondary { background: #334155; color: var(--text); }
    .btn-secondary:hover { background: #475569; }
    .btn-danger { background: var(--danger); color: white; }
    .btn-danger:hover { background: #dc2626; }
    .btn-success { background: var(--success); color: white; }
    .btn-success:hover { background: #16a34a; }
    .btn-sm { padding: 0.25rem 0.6rem; font-size: 0.8rem; }
    .status-row { display: flex; justify-content: space-between; padding: 0.5rem 0; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.9rem; }
    .status-row span:first-child { color: var(--text-muted); }
    
    /* Custom Sleek Dropdown & Perm Box */
    .perm-card-box {
      background: rgba(15, 23, 42, 0.7);
      border: 1px solid #334155;
      border-radius: 10px;
      padding: 0.85rem;
      margin-top: 1rem;
      transition: border-color 0.2s;
    }
    .perm-card-box:focus-within {
      border-color: var(--primary);
    }
    .perm-header-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.6rem;
    }
    .perm-title {
      font-weight: 600;
      font-size: 0.88rem;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.4rem;
    }
    .custom-select-wrapper {
      position: relative;
      width: 100%;
    }
    .custom-select-wrapper::after {
      content: "▾";
      font-size: 0.9rem;
      color: var(--primary);
      position: absolute;
      right: 0.9rem;
      top: 50%;
      transform: translateY(-50%);
      pointer-events: none;
    }
    .custom-select {
      width: 100%;
      appearance: none;
      -webkit-appearance: none;
      -moz-appearance: none;
      background: #090d16;
      border: 1px solid #334155;
      color: var(--text);
      border-radius: 8px;
      padding: 0.65rem 2.2rem 0.65rem 0.85rem;
      font-size: 0.88rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s ease;
      outline: none;
    }
    .custom-select:hover {
      border-color: #475569;
      background: #0f172a;
    }
    .custom-select:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
    }
    .perm-desc-box {
      margin-top: 0.6rem;
      padding: 0.5rem 0.65rem;
      border-radius: 6px;
      background: rgba(30, 41, 59, 0.5);
      border-left: 3px solid var(--primary);
      font-size: 0.78rem;
      color: var(--text-muted);
      line-height: 1.4;
      display: flex;
      align-items: flex-start;
      gap: 0.4rem;
    }

    /* Tabs */
    .tab-btn { background: none; border: none; color: var(--text-muted); font-size: 0.9rem; font-weight: 600; padding: 0.5rem 1rem; cursor: pointer; border-bottom: 2px solid transparent; }
    .tab-btn.active { color: var(--primary); border-bottom-color: var(--primary); }
    .tab-content { display: none; margin-top: 1rem; }
    .tab-content.active { display: block; }

    .progress-bar-bg { background: #334155; border-radius: 9999px; height: 10px; width: 100%; overflow: hidden; margin-top: 0.4rem; }
    .progress-bar-fill { height: 100%; border-radius: 9999px; transition: width 0.5s ease-in-out; }
    .terminal { background: var(--terminal-bg); border: 1px solid #1e293b; border-radius: 8px; font-family: monospace; font-size: 0.82rem; padding: 1rem; height: 260px; overflow-y: auto; color: #a5b4fc; white-space: pre-wrap; line-height: 1.4; }
    .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 50; align-items: center; justify-content: center; backdrop-filter: blur(4px); }
    .modal.active { display: flex; }
    .modal-content { background: var(--card); border: 1px solid var(--card-border); border-radius: 12px; width: 100%; max-width: 540px; padding: 1.5rem; max-height: 90vh; overflow-y: auto; }
    .form-group { margin-bottom: 1rem; }
    .form-group label { display: block; font-size: 0.85rem; font-weight: 500; margin-bottom: 0.4rem; color: var(--text-muted); }
    .form-control { width: 100%; background: #0f172a; border: 1px solid var(--card-border); color: var(--text); border-radius: 6px; padding: 0.5rem 0.75rem; font-size: 0.9rem; }
    textarea.form-control { resize: vertical; height: 100px; font-family: monospace; }
    .profile-item { display: flex; justify-content: space-between; align-items: center; padding: 0.75rem 0.85rem; background: #090d16; border: 1px solid var(--card-border); border-radius: 8px; margin-bottom: 0.5rem; transition: all 0.2s ease; }
    .profile-item:hover { border-color: #475569; background: #0f172a; }
    .profile-avatar { width: 32px; height: 32px; border-radius: 50%; background: #1e293b; border: 1px solid #334155; display: flex; align-items: center; justify-content: center; font-weight: 700; color: var(--primary); font-size: 0.85rem; flex-shrink: 0; }
    .profile-avatar.active { background: rgba(34, 197, 94, 0.15); border-color: rgba(34, 197, 94, 0.4); color: var(--success); }
    .profile-info { display: flex; flex-direction: column; gap: 0.15rem; }
    .profile-name { font-weight: 600; font-size: 0.92rem; }
    .profile-email { font-size: 0.78rem; color: var(--text-muted); }
    .btn-switch { background: rgba(56, 189, 248, 0.1); color: var(--primary); border: 1px solid rgba(56, 189, 248, 0.3); padding: 0.35rem 0.7rem; border-radius: 6px; font-size: 0.8rem; font-weight: 600; cursor: pointer; display: inline-flex; align-items: center; gap: 0.35rem; transition: all 0.2s ease; outline: none; }
    .btn-switch:hover { background: var(--primary); color: #0f172a; border-color: var(--primary); box-shadow: 0 0 10px rgba(56, 189, 248, 0.25); }
    .btn-trash { background: rgba(239, 68, 68, 0.1); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); padding: 0.35rem 0.55rem; border-radius: 6px; font-size: 0.8rem; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; transition: all 0.2s ease; outline: none; }
    .btn-trash:hover { background: var(--danger); color: #ffffff; border-color: var(--danger); box-shadow: 0 0 10px rgba(239, 68, 68, 0.25); }
    .toast { position: fixed; bottom: 2rem; right: 2rem; background: #1e293b; border: 1px solid var(--card-border); color: white; padding: 0.75rem 1.25rem; border-radius: 8px; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5); z-index: 100; opacity: 0; transition: opacity 0.3s; pointer-events: none; }
    .toast.show { opacity: 1; }
    .alert-box { display: flex; align-items: flex-start; gap: 0.75rem; padding: 0.85rem 1.1rem; border-radius: 8px; margin-bottom: 1.25rem; font-size: 0.88rem; line-height: 1.5; }
    .alert-danger { background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.45); color: #fca5a5; }
    .quota-card-critical { border: 1px solid var(--danger) !important; box-shadow: 0 0 12px rgba(239, 68, 68, 0.25); }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="logo">
        <span class="logo-badge">AGY</span>
        <div>
          <h1>Antigravity Manager</h1>
          <p style="color: var(--text-muted); font-size: 0.85rem;">OpenMediaVault 7 Control Hub</p>
        </div>
      </div>
      <div style="display: flex; align-items: center; gap: 0.75rem;">
        <span id="user-display" style="font-size: 0.85rem; color: var(--text-muted); font-weight: 600;"></span>
        <button class="btn btn-secondary btn-sm" onclick="refreshAll()">🔄 Refresh All</button>
        <button id="btn-change-pwd" class="btn btn-secondary btn-sm" onclick="openChangePwdModal()" style="display: none;">🔑 Đổi mật khẩu</button>
        <button id="btn-logout" class="btn btn-danger btn-sm" onclick="logout()" style="display: none;">Đăng xuất</button>
      </div>
    </header>

    <div class="grid">
      <!-- Remote Control Status Card -->
      <div class="card">
        <div class="card-title">
          <span>📡 CLI Remote-Control Daemon</span>
          <span id="daemon-badge" class="badge badge-warning">Checking...</span>
        </div>
        <div class="status-row"><span>Instance Name</span><strong id="inst-name">nas-duinch</strong></div>
        <div class="status-row"><span>PID</span><span id="daemon-pid">-</span></div>
        <div class="status-row"><span>Memory Usage</span><span id="daemon-mem">-</span></div>
        <div class="status-row"><span>CPU Usage</span><span id="daemon-cpu">-</span></div>
        <div class="status-row"><span>Active Since</span><span id="daemon-uptime">-</span></div>
        
        <!-- Permission Mode Section -->
        <div class="perm-card-box">
          <div class="perm-header-row">
            <span class="perm-title">🛡️ AGY Permission Mode</span>
            <span id="perm-badge" class="badge badge-success">⚡ Full Auto</span>
          </div>
          <div class="custom-select-wrapper">
            <select id="perm-select" class="custom-select" onchange="changePermissionMode(this.value)">
              <option value="always-proceed">⚡ always-proceed — Full Auto (Tự động duyệt mọi tool & lệnh)</option>
              <option value="agent-decides">🤖 agent-decides — AI Quyết Định (Tự đánh giá mức độ an toàn)</option>
              <option value="request-review">🛡️ request-review — Hỏi Xác Nhận (Yêu cầu review trước khi chạy)</option>
              <option value="proceed-in-sandbox">📦 proceed-in-sandbox — Cô Lập Sandbox (Thực thi trong Sandbox)</option>
            </select>
          </div>
          <div class="perm-desc-box">
            <span>💡</span>
            <span id="perm-desc">Tự động duyệt và thực thi mọi tool/lệnh CLI mà không cần hỏi xác nhận (kèm cờ --dangerously-skip-permissions).</span>
          </div>
        </div>
        
        <div style="display: flex; gap: 0.6rem; margin-top: 1.25rem;">
          <button id="btn-start" class="btn btn-success" style="display: none;" onclick="requestDaemonAction('start')">▶ Start</button>
          <button id="btn-stop" class="btn btn-danger" style="display: none;" onclick="requestDaemonAction('stop')">⏹ Stop</button>
          <button id="btn-restart" class="btn btn-secondary" style="display: none;" onclick="requestDaemonAction('restart')">🔄 Restart</button>
        </div>
      </div>

      <!-- Account & Profile Card -->
      <div class="card">
        <div class="card-title">
          <span>👤 Quản Lý Tài Khoản (Account)</span>
          <button class="btn btn-sm" onclick="openAddModal()">➕ Thêm Acc</button>
        </div>
        <div class="status-row"><span>Active Account</span><strong id="active-acc" style="color: var(--primary);">-</strong></div>
        <div class="status-row"><span>Token Hết Hạn</span><span id="token-exp">-</span></div>
        
        <div style="margin-top: 1rem;">
          <label style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.5rem; display: block;">Profiles Đã Lưu:</label>
          <div id="profiles-list" style="max-height: 180px; overflow-y: auto;">
            <!-- Rendered by JS -->
          </div>
        </div>
        <div style="margin-top: 1rem; display: flex; gap: 0.5rem;">
          <button class="btn btn-secondary btn-sm" onclick="openSaveModal()">💾 Lưu Profile Hiện Tại</button>
        </div>
      </div>
    </div>

    <!-- Usage & Quota Card -->
    <div class="card" style="margin-bottom: 1.5rem;">
      <div class="card-title" style="flex-wrap: wrap; gap: 0.75rem;">
        <div style="display: flex; align-items: center; gap: 0.6rem;">
          <span>📊 AGY Model Quota & Usage</span>
          <span id="quota-warning-badge" class="badge badge-danger" style="display: none; font-size: 0.75rem;">⚠️ Low Quota (&lt; 20%)</span>
        </div>
        <div style="display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap;">
          <span id="usage-time" style="font-size: 0.8rem; color: var(--text-muted);"></span>
          
          <!-- Interval pull selector -->
          <div style="display: flex; align-items: center; gap: 0.35rem; font-size: 0.8rem; color: var(--text-muted); background: #0f172a; padding: 0.25rem 0.55rem; border-radius: 6px; border: 1px solid var(--card-border);">
            <span>⏱️ Chu kỳ kéo:</span>
            <select id="usage-interval" onchange="changeUsageInterval(this.value)" style="background: transparent; border: none; color: var(--primary); font-size: 0.8rem; font-weight: 600; outline: none; cursor: pointer;">
              <option value="0">Tắt (Thủ công)</option>
              <option value="15">15s</option>
              <option value="30">30s</option>
              <option value="60" selected>1m</option>
              <option value="120">2m</option>
              <option value="300">5m</option>
            </select>
          </div>

          <button id="btn-refresh-usage" class="btn btn-sm" onclick="fetchUsage(false)">🔄 Truy Vấn Usage</button>
        </div>
      </div>

      <!-- Low Quota Alert Banner -->
      <div id="low-quota-alert" class="alert-box alert-danger" style="display: none;">
        <span style="font-size: 1.4rem;">⚠️</span>
        <div style="flex: 1;">
          <strong style="color: #f87171; font-size: 0.95rem;">CẢNH BÁO: Mức Quota / Token Đang Dưới 20%!</strong>
          <div id="low-quota-msg" style="margin-top: 0.35rem;"></div>
        </div>
        <button class="btn btn-sm btn-secondary" onclick="document.getElementById('low-quota-alert').style.display='none'">Đóng</button>
      </div>

      <div id="usage-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 1rem;">
        <p style="color: var(--text-muted); grid-column: 1 / -1;">Đang tải dữ liệu quota từ agy...</p>
      </div>
    </div>

    <!-- Real-time Logs Card -->
    <div class="card">
      <div class="card-title">
        <span>📜 Nhật Ký CLI Daemon (Journalctl)</span>
        <div style="display: flex; gap: 0.75rem; align-items: center;">
          <label style="font-size: 0.8rem; display: flex; align-items: center; gap: 0.3rem; cursor: pointer;">
            <input type="checkbox" id="auto-logs" checked> Tự động cuộn
          </label>
          <button class="btn btn-secondary btn-sm" onclick="fetchLogs()">Làm mới Logs</button>
        </div>
      </div>
      <div id="logs-box" class="terminal">Đang tải nhật ký...</div>
    </div>
  </div>

  <!-- Modal Add Account (with Tabs for Automated OAuth vs Manual Token) -->
  <div id="modal-add" class="modal">
    <div class="modal-content">
      <h3 style="margin-bottom: 0.75rem;">➕ Thêm Tài Khoản Mới</h3>
      
      <div style="display: flex; border-bottom: 1px solid var(--card-border); margin-bottom: 1rem;">
        <button class="tab-btn active" onclick="switchAddTab('auto')">🌐 Đăng Nhập Tự Động (OAuth)</button>
        <button class="tab-btn" onclick="switchAddTab('manual')">📋 Dán Token Thủ Công</button>
      </div>

      <!-- Tab 1: Automated Google OAuth Flow -->
      <div id="tab-auto" class="tab-content active">
        <div class="form-group">
          <label>Tên Profile:</label>
          <input type="text" id="auto-prof-name" class="form-control" placeholder="vd: Work, Personal 2">
        </div>
        <div class="form-group" style="display: flex; align-items: center; gap: 0.5rem;">
          <input type="checkbox" id="auto-prof-activate" checked>
          <label for="auto-prof-activate" style="margin-bottom: 0; cursor: pointer;">Tự động chuyển sang tài khoản này sau khi login xong</label>
        </div>

        <!-- Initial state -->
        <div id="oauth-init-box">
          <button class="btn btn-success" style="width: 100%; justify-content: center; padding: 0.75rem;" onclick="startGoogleOAuth()">
            🚀 Bắt Đầu Đăng Nhập Google (1-Click)
          </button>
          <p style="font-size: 0.8rem; color: var(--text-muted); margin-top: 0.5rem; text-align: center;">
            Hệ thống sẽ tạo liên kết ủy quyền Google chính thức để bạn đăng nhập trên trình duyệt.
          </p>
        </div>

        <!-- Waiting User step -->
        <div id="oauth-waiting-box" style="display: none; background: #0f172a; padding: 1rem; border-radius: 8px; border: 1px solid var(--card-border); margin-top: 1rem;">
          <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.75rem;">
            <span style="font-size: 1.2rem;">⏳</span>
            <strong style="color: var(--primary);">Đã tạo liên kết đăng nhập Google!</strong>
          </div>
          <p style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.75rem;">
            Bước 1: Click vào nút bên dưới để mở trang đăng nhập Google trong tab mới:
          </p>
          <a id="oauth-auth-link" href="#" target="_blank" class="btn" style="width: 100%; justify-content: center; margin-bottom: 1rem;">
            👉 Mở Trang Đăng Nhập Google
          </a>
          
          <div style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.5rem;">
            Bước 2: Sau khi đăng nhập, hệ thống sẽ <strong>tự động nhận diện token</strong>. Nếu Google hiển thị mã Authorization Code thay vì chuyển hướng:
          </div>
          <div style="display: flex; gap: 0.5rem;">
            <input type="text" id="oauth-code-input" class="form-control" placeholder="Dán mã Authorization Code vào đây (nếu có)">
            <button class="btn btn-secondary btn-sm" onclick="submitAuthCode()">Gửi Mã</button>
          </div>
          <div id="oauth-spinner" style="font-size: 0.8rem; color: var(--primary); margin-top: 0.75rem; text-align: center;">
            🔄 Đang chờ xác thực từ Google...
          </div>
        </div>
      </div>

      <!-- Tab 2: Manual Token JSON -->
      <div id="tab-manual" class="tab-content">
        <div class="form-group">
          <label>Tên Profile:</label>
          <input type="text" id="new-prof-name" class="form-control" placeholder="vd: Work, Personal 2">
        </div>
        <div class="form-group">
          <label>Nội Dung File `antigravity-oauth-token` (JSON):</label>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.4rem;">
            Lấy từ máy đã đăng nhập: <code>~/.gemini/antigravity-cli/antigravity-oauth-token</code>
          </div>
          <textarea id="new-prof-token" class="form-control" placeholder='{"token": {"access_token": "..."}, "auth_method": "consumer", "id_token": "..."}'></textarea>
        </div>
        <div class="form-group" style="display: flex; align-items: center; gap: 0.5rem;">
          <input type="checkbox" id="new-prof-activate" checked>
          <label for="new-prof-activate" style="margin-bottom: 0; cursor: pointer;">Chuyển sang dùng tài khoản này ngay</label>
        </div>
        <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem;">
          <button class="btn btn-secondary" onclick="closeModals()">Hủy</button>
          <button class="btn" onclick="submitAddAccount()">Lưu Tài Khoản</button>
        </div>
      </div>

      <div style="display: flex; justify-content: flex-end; margin-top: 1rem;">
        <button class="btn btn-secondary" onclick="closeModals()">Đóng</button>
      </div>
    </div>
  </div>

  <!-- Modal Save Profile -->
  <div id="modal-save" class="modal">
    <div class="modal-content">
      <h3 style="margin-bottom: 1rem;">💾 Lưu Profile Hiện Tại</h3>
      <div class="form-group">
        <label>Tên Profile:</label>
        <input type="text" id="save-prof-name" class="form-control" placeholder="vd: Main Google Account">
      </div>
      <div style="display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem;">
        <button class="btn btn-secondary" onclick="closeModals()">Hủy</button>
        <button class="btn" onclick="submitSaveProfile()">Lưu</button>
      </div>
    </div>
  </div>

  <!-- Modal Confirm Action -->
  <div id="modal-confirm" class="modal">
    <div class="modal-content" style="max-width: 440px;">
      <div style="display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.85rem;">
        <span id="confirm-icon" style="font-size: 1.6rem;">⚠️</span>
        <h3 id="confirm-title" style="margin-bottom: 0;">Xác nhận thao tác</h3>
      </div>
      <p id="confirm-desc" style="font-size: 0.88rem; color: var(--text-muted); line-height: 1.5; margin-bottom: 1.25rem;"></p>
      <div style="display: flex; justify-content: flex-end; gap: 0.6rem;">
        <button class="btn btn-secondary" onclick="closeConfirmModal()">Hủy</button>
        <button id="confirm-btn-action" class="btn btn-danger" onclick="executeConfirmAction()">Xác nhận</button>
      </div>
    </div>
  </div>

  <!-- Modal Login Authentication -->
  <div id="modal-login" class="modal" style="z-index: 200;">
    <div class="modal-content" style="max-width: 400px; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7); border-color: #475569;">
      <div style="text-align: center; margin-bottom: 1.5rem;">
        <span class="logo-badge" style="font-size: 1.5rem; padding: 0.5rem 1rem; border-radius: 12px; display: inline-block; margin-bottom: 0.75rem;">AGY</span>
        <h2 style="font-size: 1.3rem; font-weight: 700; color: #fff;">Đăng nhập AGY Manager</h2>
        <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.25rem;">Hệ thống OpenMediaVault Control Hub</p>
      </div>
      <form onsubmit="handleWebLogin(event)">
        <div class="form-group">
          <label>Tài khoản</label>
          <input type="text" id="web-login-user" required class="form-control" value="chungnh">
        </div>
        <div class="form-group" style="margin-bottom: 1.25rem;">
          <label>Mật khẩu</label>
          <input type="password" id="web-login-pass" required placeholder="Nhập mật khẩu" class="form-control">
        </div>
        <div id="web-login-error" style="display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #fca5a5; padding: 0.6rem; border-radius: 6px; font-size: 0.82rem; margin-bottom: 1rem; text-align: center;"></div>
        <button type="submit" class="btn btn-primary" style="width: 100%; justify-content: center; padding: 0.7rem; font-size: 0.95rem;">Đăng nhập</button>
      </form>
    </div>
  <!-- Modal Change Password -->
  <div id="modal-change-pwd" class="modal" style="z-index: 200;">
    <div class="modal-content" style="max-width: 440px; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7); border-color: #475569;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.25rem;">
        <h3 style="margin: 0; font-size: 1.15rem; color: #fff;">🔑 Đổi mật khẩu quản trị</h3>
        <button type="button" class="btn btn-secondary btn-sm" onclick="closeChangePwdModal()">✕</button>
      </div>
      <form onsubmit="handleChangePassword(event)">
        <div class="form-group">
          <label>Mật khẩu hiện tại</label>
          <input type="password" id="pwd-current" required placeholder="Nhập mật khẩu đang dùng" class="form-control">
        </div>
        <div class="form-group">
          <label>Mật khẩu mới (tối thiểu 8 ký tự)</label>
          <input type="password" id="pwd-new" required minlength="8" placeholder="Nhập mật khẩu mới" class="form-control">
        </div>
        <div class="form-group" style="margin-bottom: 1.25rem;">
          <label>Xác nhận mật khẩu mới</label>
          <input type="password" id="pwd-confirm" required minlength="8" placeholder="Nhập lại mật khẩu mới" class="form-control">
        </div>
        <div id="pwd-error" style="display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #fca5a5; padding: 0.6rem; border-radius: 6px; font-size: 0.82rem; margin-bottom: 1rem; text-align: center;"></div>
        <div style="display: flex; justify-content: flex-end; gap: 0.6rem;">
          <button type="button" class="btn btn-secondary" onclick="closeChangePwdModal()">Hủy</button>
          <button type="submit" id="btn-submit-pwd" class="btn btn-primary">Lưu mật khẩu</button>
        </div>
      </form>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    let activeOAuthSession = null;
    let oauthPollTimer = null;
    let pendingConfirmCallback = null;

    function showConfirmModal({ title, desc, icon, btnText, btnClass, onConfirm }) {
      document.getElementById('confirm-title').innerText = title || 'Xác nhận thao tác';
      document.getElementById('confirm-desc').innerHTML = desc || 'Bạn có chắc chắn muốn thực hiện hành động này?';
      document.getElementById('confirm-icon').innerText = icon || '⚠️';
      
      const btn = document.getElementById('confirm-btn-action');
      btn.innerText = btnText || 'Xác nhận';
      btn.className = 'btn ' + (btnClass || 'btn-danger');
      
      pendingConfirmCallback = onConfirm;
      document.getElementById('modal-confirm').classList.add('active');
    }

    function closeConfirmModal() {
      document.getElementById('modal-confirm').classList.remove('active');
      pendingConfirmCallback = null;
    }

    function executeConfirmAction() {
      if (typeof pendingConfirmCallback === 'function') {
        const cb = pendingConfirmCallback;
        closeConfirmModal();
        cb();
      } else {
        closeConfirmModal();
      }
    }

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 3500);
    }

    function switchAddTab(tab) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      if (tab === 'auto') {
        document.querySelectorAll('.tab-btn')[0].classList.add('active');
        document.getElementById('tab-auto').classList.add('active');
      } else {
        document.querySelectorAll('.tab-btn')[1].classList.add('active');
        document.getElementById('tab-manual').classList.add('active');
      }
    }

    function openAddModal() { 
      document.getElementById('modal-add').classList.add('active'); 
      document.getElementById('oauth-init-box').style.display = 'block';
      document.getElementById('oauth-waiting-box').style.display = 'none';
      if (oauthPollTimer) clearInterval(oauthPollTimer);
    }
    function openSaveModal() { document.getElementById('modal-save').classList.add('active'); }
    function closeModals() { 
      document.querySelectorAll('.modal:not(#modal-login)').forEach(m => m.classList.remove('active')); 
      if (oauthPollTimer) clearInterval(oauthPollTimer);
      if (activeOAuthSession) {
        fetch(`/api/auth/cancel?session_id=${activeOAuthSession}`, { method: 'POST' });
        activeOAuthSession = null;
      }
      pendingConfirmCallback = null;
    }

    async function startGoogleOAuth() {
      const name = document.getElementById('auto-prof-name').value.trim() || "Google Account";
      const activate = document.getElementById('auto-prof-activate').checked;
      showToast("Đang khởi tạo phiên đăng nhập Google...");

      try {
        const res = await fetch('/api/auth/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile_name: name, activate_now: activate })
        });
        const data = await res.json();
        if (data.success && data.auth_url) {
          activeOAuthSession = data.session_id;
          document.getElementById('oauth-init-box').style.display = 'none';
          document.getElementById('oauth-waiting-box').style.display = 'block';
          document.getElementById('oauth-auth-link').href = data.auth_url;

          // Start polling for token completion
          if (oauthPollTimer) clearInterval(oauthPollTimer);
          oauthPollTimer = setInterval(pollOAuthStatus, 2000);
        } else {
          alert("Không lấy được URL xác thực: " + (data.error || "Unknown"));
        }
      } catch (e) {
        alert("Lỗi kết nối: " + e);
      }
    }

    async function pollOAuthStatus() {
      if (!activeOAuthSession) return;
      try {
        const res = await fetch(`/api/auth/status?session_id=${activeOAuthSession}`);
        const data = await res.json();

        if (data.status === 'completed') {
          clearInterval(oauthPollTimer);
          activeOAuthSession = null;
          showToast(`✅ Đăng nhập thành công tài khoản: ${data.email}!`);
          closeModals();
          setTimeout(fetchStatus, 1000);
        } else if (data.status === 'failed' || data.status === 'timeout') {
          clearInterval(oauthPollTimer);
          alert(`Đăng nhập không thành công: ${data.error}`);
          document.getElementById('oauth-init-box').style.display = 'block';
          document.getElementById('oauth-waiting-box').style.display = 'none';
        }
      } catch (e) {
        console.error("pollOAuthStatus error", e);
      }
    }

    async function submitAuthCode() {
      const code = document.getElementById('oauth-code-input').value.trim();
      if (!code || !activeOAuthSession) {
        alert("Vui lòng nhập mã Authorization Code");
        return;
      }
      try {
        const res = await fetch('/api/auth/submit_code', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: activeOAuthSession, code: code })
        });
        const data = await res.json();
        showToast(data.message);
      } catch (e) {
        alert("Lỗi: " + e);
      }
    }

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        if (res.status === 401) {
          currentUser = null;
          document.getElementById('modal-login').classList.add('active');
          const logoutBtn = document.getElementById('btn-logout');
          if (logoutBtn) logoutBtn.style.display = 'none';
          return;
        }
        const data = await res.json();
        
        // Daemon info & button visibility
        const badge = document.getElementById('daemon-badge');
        const btnStart = document.getElementById('btn-start');
        const btnStop = document.getElementById('btn-stop');
        const btnRestart = document.getElementById('btn-restart');

        if (data.daemon.active) {
          badge.className = 'badge badge-success';
          badge.innerText = '🟢 RUNNING';
          if (btnStart) btnStart.style.display = 'none';
          if (btnStop) {
            btnStop.style.display = 'inline-flex';
            btnStop.disabled = false;
          }
          if (btnRestart) {
            btnRestart.style.display = 'inline-flex';
            btnRestart.disabled = false;
          }
        } else {
          badge.className = 'badge badge-danger';
          badge.innerText = '🔴 STOPPED';
          if (btnStart) {
            btnStart.style.display = 'inline-flex';
            btnStart.disabled = false;
          }
          if (btnStop) btnStop.style.display = 'none';
          if (btnRestart) btnRestart.style.display = 'none';
        }
        document.getElementById('daemon-pid').innerText = data.daemon.pid;
        document.getElementById('daemon-mem').innerText = data.daemon.memory;
        document.getElementById('daemon-cpu').innerText = data.daemon.cpu;
        document.getElementById('daemon-uptime').innerText = data.daemon.active_since;
        document.getElementById('inst-name').innerText = data.daemon.instance_name;

        // Permissions dropdown & badge
        if (data.permissions && data.permissions.current_mode) {
          updatePermUI(data.permissions.current_mode);
        }

        // Account info
        document.getElementById('active-acc').innerText = data.account.email;
        const expElem = document.getElementById('token-exp');
        const expText = data.account.expiry || '-';
        if (expText.toLowerCase().includes('expired') || expText.toLowerCase().includes('hết hạn')) {
          expElem.innerHTML = `<span style="color: var(--danger); font-weight: 600;">⚠️ ${expText} (Token hết hạn)</span>`;
        } else {
          expElem.innerText = expText;
        }

        // Profiles
        renderProfiles(data.profiles, data.account.email);
      } catch (e) {
        console.error("fetchStatus error", e);
      }
    }

    const PERM_DESCRIPTIONS = {
      'always-proceed': 'Tự động duyệt và thực thi mọi tool/lệnh CLI mà không cần hỏi xác nhận (kèm cờ --dangerously-skip-permissions).',
      'agent-decides': 'AI agent tự đánh giá mức độ an toàn để quyết định tự thực thi hay hỏi người dùng duyệt.',
      'request-review': 'Luôn yêu cầu xác nhận trước khi thực hiện các thao tác chỉnh sửa file hoặc lệnh terminal.',
      'proceed-in-sandbox': 'Tự động thực thi an toàn trong môi trường sandbox cô lập bị giới hạn truy cập.'
    };

    const PERM_BADGES = {
      'always-proceed': { text: '⚡ Full Auto', cls: 'badge-success' },
      'agent-decides': { text: '🤖 AI Decides', cls: 'badge-info' },
      'request-review': { text: '🛡️ Review', cls: 'badge-warning' },
      'proceed-in-sandbox': { text: '📦 Sandbox', cls: 'badge-secondary' }
    };

    function updatePermUI(mode) {
      const select = document.getElementById('perm-select');
      const badge = document.getElementById('perm-badge');
      const desc = document.getElementById('perm-desc');
      
      if (select && document.activeElement !== select) {
        select.value = mode;
      }
      if (desc && PERM_DESCRIPTIONS[mode]) {
        desc.innerText = PERM_DESCRIPTIONS[mode];
      }
      if (badge && PERM_BADGES[mode]) {
        badge.className = 'badge ' + PERM_BADGES[mode].cls;
        badge.innerText = PERM_BADGES[mode].text;
      }
    }

    async function changePermissionMode(mode) {
      showToast(`Đang chuyển sang mode: ${mode}...`);
      try {
        const res = await fetch('/api/permissions/mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: mode })
        });
        const data = await res.json();
        showToast(data.message);
        updatePermUI(mode);
        setTimeout(fetchStatus, 1500);
      } catch (e) {
        showToast("Error: " + e);
        fetchStatus();
      }
    }

    function renderProfiles(profilesData, activeEmail) {
      const list = document.getElementById('profiles-list');
      list.innerHTML = '';
      const activeId = profilesData.active_profile;
      const profiles = profilesData.profiles || [];

      if (profiles.length === 0) {
        list.innerHTML = '<div style="font-size:0.8rem; color:var(--text-muted); text-align:center; padding:0.75rem;">Chưa có profile nào được lưu.</div>';
        return;
      }

      profiles.forEach(p => {
        const isActive = (p.id === activeId);
        const safeName = (p.name || '').replace(/'/g, "\\'");
        const displayName = p.name || p.email || 'Profile';
        const initial = displayName.charAt(0).toUpperCase();
        const isSame = p.name && p.email && (p.name.trim().toLowerCase() === p.email.trim().toLowerCase());

        const div = document.createElement('div');
        div.className = 'profile-item';
        div.innerHTML = `
          <div style="display: flex; align-items: center; gap: 0.65rem; min-width: 0; flex: 1;">
            <div class="profile-avatar ${isActive ? 'active' : ''}">${initial}</div>
            <div class="profile-info" style="min-width: 0; flex: 1;">
              <div style="display: flex; align-items: center; gap: 0.45rem; overflow: hidden;">
                <span class="profile-name" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${displayName}</span>
                ${isActive ? `
                  <span class="badge badge-success" style="font-size: 0.68rem; padding: 0.15rem 0.45rem; white-space: nowrap;">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                    Đang dùng
                  </span>
                ` : ''}
              </div>
              ${!isSame && p.email ? `<span class="profile-email" style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${p.email}</span>` : ''}
            </div>
          </div>
          <div style="display: flex; gap: 0.45rem; align-items: center; margin-left: 0.5rem; flex-shrink: 0;">
            ${!isActive ? `
              <button class="btn-switch" onclick="switchProfile('${p.id}', '${safeName}')" title="Chuyển sang dùng tài khoản này">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m16 3 4 4-4 4"/><path d="M20 7H4"/><path d="m8 21-4-4 4-4"/><path d="M4 17h16"/></svg>
                Switch
              </button>
              <button class="btn-trash" onclick="deleteProfile('${p.id}', '${safeName}')" title="Xóa tài khoản này">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg>
              </button>
            ` : ''}
          </div>
        `;
        list.appendChild(div);
      });
    }

    function deleteProfile(id, name) {
      showConfirmModal({
        title: 'Xác nhận Xóa Profile',
        icon: '🗑️',
        desc: `Bạn có chắc chắn muốn xóa profile <strong>${name || id}</strong>?<br>Dữ liệu đăng nhập lưu trữ cho profile này sẽ bị gỡ bỏ hoàn toàn.`,
        btnText: '🗑️ Xóa Profile',
        btnClass: 'btn-danger',
        onConfirm: async () => {
          try {
            const res = await fetch('/api/accounts/delete', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ profile_id: id })
            });
            const data = await res.json();
            showToast(data.message);
            fetchStatus();
          } catch (e) {
            showToast("Error: " + e);
          }
        }
      });
    }

    function requestDaemonAction(action) {
      if (action === 'start') {
        controlDaemon('start');
      } else if (action === 'stop') {
        showConfirmModal({
          title: 'Xác nhận Dừng Daemon',
          icon: '⏹️',
          desc: 'Bạn có chắc chắn muốn dừng dịch vụ <code>antigravity-cli-daemon</code>?<br>Mọi phiên làm việc Antigravity từ xa hoặc tiến trình đang chạy sẽ bị ngắt kết nối.',
          btnText: '⏹ Dừng Daemon',
          btnClass: 'btn-danger',
          onConfirm: () => controlDaemon('stop')
        });
      } else if (action === 'restart') {
        showConfirmModal({
          title: 'Xác nhận Khởi Động Lại Daemon',
          icon: '🔄',
          desc: 'Bạn có chắc chắn muốn khởi động lại dịch vụ <code>antigravity-cli-daemon</code>?<br>Dịch vụ sẽ tải lại cấu hình và khởi tạo phiên kết nối mới.',
          btnText: '🔄 Khởi Động Lại',
          btnClass: 'btn-warning',
          onConfirm: () => controlDaemon('restart')
        });
      }
    }

    async function controlDaemon(action) {
      showToast(`Đang thực hiện ${action} daemon...`);
      try {
        const res = await fetch(`/api/daemon/${action}`, { method: 'POST' });
        const data = await res.json();
        showToast(data.message || "Done");
        setTimeout(fetchStatus, 1000);
        fetchLogs();
      } catch (e) {
        showToast("Error: " + e);
      }
    }

    function switchProfile(id, name) {
      showConfirmModal({
        title: 'Chuyển Tài Khoản (Profile)',
        icon: '👤',
        desc: `Bạn có chắc muốn chuyển sang profile <strong>${name || id}</strong>?<br>Dịch vụ remote-control daemon sẽ tự động khởi động lại để áp dụng token mới.`,
        btnText: 'Chuyển Profile',
        btnClass: 'btn-primary',
        onConfirm: async () => {
          showToast("Đang chuyển profile...");
          try {
            const res = await fetch('/api/accounts/switch', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ profile_id: id })
            });
            const data = await res.json();
            showToast(data.message);
            setTimeout(fetchStatus, 1500);
          } catch (e) {
            showToast("Error: " + e);
          }
        }
      });
    }

    async function submitAddAccount() {
      const name = document.getElementById('new-prof-name').value.trim();
      const token = document.getElementById('new-prof-token').value.trim();
      const activate = document.getElementById('new-prof-activate').checked;
      if (!name || !token) { alert("Vui lòng điền đủ tên và token JSON"); return; }

      try {
        const res = await fetch('/api/accounts/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, token_json: token, activate_now: activate })
        });
        const data = await res.json();
        if (data.success) {
          showToast(data.message);
          closeModals();
          setTimeout(fetchStatus, 1000);
        } else {
          alert(data.message);
        }
      } catch (e) {
        alert("Lỗi: " + e);
      }
    }

    async function submitSaveProfile() {
      const name = document.getElementById('save-prof-name').value.trim();
      if (!name) { alert("Vui lòng nhập tên profile"); return; }
      try {
        const res = await fetch('/api/accounts/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name })
        });
        const data = await res.json();
        showToast(data.message);
        closeModals();
        fetchStatus();
      } catch (e) {
        alert("Lỗi: " + e);
      }
    }

    let usageIntervalTimer = null;
    let lastLowQuotaWarning = 0;

    function changeUsageInterval(seconds) {
      seconds = parseInt(seconds) || 0;
      localStorage.setItem('agy_usage_interval', seconds);
      if (usageIntervalTimer) {
        clearInterval(usageIntervalTimer);
        usageIntervalTimer = null;
      }
      if (seconds > 0) {
        usageIntervalTimer = setInterval(() => fetchUsage(true), seconds * 1000);
        showToast(`Tự động kéo Quota mỗi ${seconds < 60 ? seconds + 's' : (seconds / 60) + 'm'}`);
      } else {
        showToast("Đã tắt tự động kéo Quota (Chuyển sang chế độ thủ công)");
      }
    }

    async function fetchUsage(isAuto = false) {
      const btn = document.getElementById('btn-refresh-usage');
      if (!isAuto && btn) {
        btn.disabled = true;
        btn.innerText = "⏳ Đang truy vấn...";
      }
      try {
        const res = await fetch('/api/usage');
        const data = await res.json();
        if (btn) {
          btn.disabled = false;
          btn.innerText = "🔄 Truy Vấn Usage";
        }
        
        if (data.error) {
          if (!isAuto) showToast("Lỗi: " + data.error);
          return;
        }

        const intervalSelect = document.getElementById('usage-interval');
        const intervalVal = intervalSelect ? parseInt(intervalSelect.value) : 0;
        const intervalText = intervalVal > 0 ? ` (Tự động ${intervalVal < 60 ? intervalVal + 's' : (intervalVal / 60) + 'm'})` : '';
        document.getElementById('usage-time').innerText = "Cập nhật: " + data.queried_at + intervalText;

        const grid = document.getElementById('usage-grid');
        grid.innerHTML = '';
        
        let lowQuotas = [];

        (data.quotas || []).forEach(q => {
          let color = 'var(--success)';
          let isCritical = false;
          if (q.percentage < 20) {
            color = 'var(--danger)';
            isCritical = true;
            lowQuotas.push(q);
          } else if (q.percentage < 50) {
            color = 'var(--warning)';
          }

          const card = document.createElement('div');
          card.style.background = '#0f172a';
          card.style.padding = '1rem';
          card.style.borderRadius = '8px';
          card.style.border = isCritical ? '1px solid var(--danger)' : '1px solid var(--card-border)';
          if (isCritical) {
            card.className = 'quota-card-critical';
          }
          card.innerHTML = `
            <div style="display:flex; justify-content:space-between; align-items:center; font-weight:600; font-size:0.95rem; margin-bottom:0.25rem;">
              <span>${q.model_group}</span>
              <span style="color: ${color}; display:flex; align-items:center; gap:0.3rem;">
                ${isCritical ? '⚠️ ' : ''}${q.percentage}% Còn lại
              </span>
            </div>
            <div style="font-size:0.8rem; color:var(--text-muted); margin-bottom:0.5rem;">${q.limit_type}</div>
            <div class="progress-bar-bg">
              <div class="progress-bar-fill" style="width: ${q.percentage}%; background: ${color};"></div>
            </div>
            <div style="display:flex; justify-content:space-between; align-items:center; font-size:0.75rem; color:var(--text-muted); margin-top:0.4rem;">
              <span>Reset: ${q.reset_time}</span>
              ${isCritical ? '<strong style="color:var(--danger)">Cạn kiệt token!</strong>' : ''}
            </div>
          `;
          grid.appendChild(card);
        });

        // Low Token / Quota Alert Handling
        const alertBox = document.getElementById('low-quota-alert');
        const alertMsg = document.getElementById('low-quota-msg');
        const warningBadge = document.getElementById('quota-warning-badge');

        if (lowQuotas.length > 0) {
          if (warningBadge) warningBadge.style.display = 'inline-flex';
          if (alertBox && alertMsg) {
            const listItems = lowQuotas.map(q => `• <strong>${q.model_group}</strong>: chỉ còn <strong>${q.percentage}%</strong> (Reset lúc ${q.reset_time})`).join('<br>');
            alertMsg.innerHTML = `${listItems}<br><span style="color: #cbd5e1; font-size: 0.82rem; margin-top: 0.4rem; display: block;">💡 <strong>Khuyến nghị:</strong> Chuyển sang profile tài khoản dự phòng khác ở mục "Quản Lý Tài Khoản" bên trên.</span>`;
            alertBox.style.display = 'flex';
          }

          const now = Date.now();
          if (!isAuto || (now - lastLowQuotaWarning > 180000)) { // alert every 3 mins if auto
            showToast(`⚠️ CẢNH BÁO: ${lowQuotas.length} model quota đã xuống dưới 20%!`);
            lastLowQuotaWarning = now;
          }
        } else {
          if (warningBadge) warningBadge.style.display = 'none';
          if (alertBox) alertBox.style.display = 'none';
        }

      } catch (e) {
        if (btn) {
          btn.disabled = false;
          btn.innerText = "🔄 Truy Vấn Usage";
        }
        if (!isAuto) showToast("Error: " + e);
      }
    }

    async function fetchLogs() {
      try {
        const res = await fetch('/api/logs');
        const text = await res.text();
        const box = document.getElementById('logs-box');
        box.innerText = text;
        if (document.getElementById('auto-logs').checked) {
          box.scrollTop = box.scrollHeight;
        }
      } catch (e) {
        console.error("fetchLogs error", e);
      }
    }

    let currentUser = null;
    let initialTimersStarted = false;

    async function checkAuthAndInit() {
      try {
        const res = await fetch('/api/me');
        const data = await res.json();
        if (data.authenticated) {
          currentUser = data.user;
          document.getElementById('modal-login').classList.remove('active');
          const userDisplay = document.getElementById('user-display');
          if (userDisplay) userDisplay.innerText = `👤 ${data.user}`;
          const logoutBtn = document.getElementById('btn-logout');
          if (logoutBtn) logoutBtn.style.display = 'inline-flex';
          const changePwdBtn = document.getElementById('btn-change-pwd');
          if (changePwdBtn) changePwdBtn.style.display = 'inline-flex';
          startAppTimers();
        } else {
          document.getElementById('modal-login').classList.add('active');
          const logoutBtn = document.getElementById('btn-logout');
          if (logoutBtn) logoutBtn.style.display = 'none';
          const changePwdBtn = document.getElementById('btn-change-pwd');
          if (changePwdBtn) changePwdBtn.style.display = 'none';
        }
      } catch (err) {
        document.getElementById('modal-login').classList.add('active');
      }
    }

    async function handleWebLogin(e) {
      e.preventDefault();
      const u = document.getElementById('web-login-user').value.trim();
      const p = document.getElementById('web-login-pass').value;
      const errEl = document.getElementById('web-login-error');
      errEl.style.display = 'none';
      try {
        const res = await fetch('/api/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: u, password: p })
        });
        const data = await res.json();
        if (data.success) {
          currentUser = data.user;
          document.getElementById('modal-login').classList.remove('active');
          const userDisplay = document.getElementById('user-display');
          if (userDisplay) userDisplay.innerText = `👤 ${data.user}`;
          const logoutBtn = document.getElementById('btn-logout');
          if (logoutBtn) logoutBtn.style.display = 'inline-flex';
          const changePwdBtn = document.getElementById('btn-change-pwd');
          if (changePwdBtn) changePwdBtn.style.display = 'inline-flex';
          startAppTimers();
          refreshAll();
        } else {
          errEl.innerText = data.message || 'Tên đăng nhập hoặc mật khẩu không đúng';
          errEl.style.display = 'block';
        }
      } catch (err) {
        errEl.innerText = 'Lỗi kết nối máy chủ: ' + err.message;
        errEl.style.display = 'block';
      }
    }

    async function logout() {
      try {
        await fetch('/api/logout', { method: 'POST' });
      } catch (e) {}
      currentUser = null;
      document.getElementById('modal-login').classList.add('active');
      const passInput = document.getElementById('web-login-pass');
      if (passInput) passInput.value = '';
      const logoutBtn = document.getElementById('btn-logout');
      if (logoutBtn) logoutBtn.style.display = 'none';
      const changePwdBtn = document.getElementById('btn-change-pwd');
      if (changePwdBtn) changePwdBtn.style.display = 'none';
      const userDisplay = document.getElementById('user-display');
      if (userDisplay) userDisplay.innerText = '';
    }

    function openChangePwdModal() {
      document.getElementById('pwd-current').value = '';
      document.getElementById('pwd-new').value = '';
      document.getElementById('pwd-confirm').value = '';
      document.getElementById('pwd-error').style.display = 'none';
      document.getElementById('modal-change-pwd').classList.add('active');
    }

    function closeChangePwdModal() {
      document.getElementById('modal-change-pwd').classList.remove('active');
    }

    async function handleChangePassword(e) {
      e.preventDefault();
      const currentPass = document.getElementById('pwd-current').value;
      const newPass = document.getElementById('pwd-new').value;
      const confirmPass = document.getElementById('pwd-confirm').value;
      const errEl = document.getElementById('pwd-error');
      errEl.style.display = 'none';

      if (newPass.length < 8) {
        errEl.innerText = 'Mật khẩu mới phải có tối thiểu 8 ký tự.';
        errEl.style.display = 'block';
        return;
      }
      if (newPass !== confirmPass) {
        errEl.innerText = 'Mật khẩu mới và xác nhận mật khẩu không khớp.';
        errEl.style.display = 'block';
        return;
      }

      const btn = document.getElementById('btn-submit-pwd');
      btn.disabled = true;
      btn.innerText = 'Đang lưu...';

      try {
        const res = await fetch('/api/change-password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            current_password: currentPass,
            new_password: newPass
          })
        });
        const data = await res.json();
        if (data.success) {
          closeChangePwdModal();
          showToast('✅ Đổi mật khẩu thành công!');
        } else {
          errEl.innerText = data.message || 'Lỗi khi đổi mật khẩu.';
          errEl.style.display = 'block';
        }
      } catch (err) {
        errEl.innerText = 'Lỗi kết nối máy chủ: ' + err.message;
        errEl.style.display = 'block';
      } finally {
        btn.disabled = false;
        btn.innerText = 'Lưu mật khẩu';
      }
    }

    function startAppTimers() {
      if (initialTimersStarted) return;
      initialTimersStarted = true;
      fetchStatus();
      fetchLogs();
      setInterval(fetchStatus, 5000);
      setInterval(fetchLogs, 4000);

      const savedInterval = localStorage.getItem('agy_usage_interval');
      const initialInterval = (savedInterval !== null) ? parseInt(savedInterval) : 60;
      const intervalSelectElem = document.getElementById('usage-interval');
      if (intervalSelectElem) {
        intervalSelectElem.value = initialInterval;
      }
      if (initialInterval > 0) {
        usageIntervalTimer = setInterval(() => fetchUsage(true), initialInterval * 1000);
      }
      setTimeout(() => fetchUsage(true), 500);
    }

    function refreshAll() {
      if (!currentUser) return;
      fetchStatus();
      fetchLogs();
      fetchUsage(false);
      showToast("Đã làm mới toàn bộ dữ liệu");
    }

    // Check auth on page load
    checkAuthAndInit();
  </script>
</body>
</html>
"""

class RequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def _send_text(self, text, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(text.encode('utf-8'))

    def _send_html(self, html, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def _get_cookie(self, name):
        cookie_header = self.headers.get('Cookie')
        if not cookie_header:
            return None
        try:
            cookie = SimpleCookie(cookie_header)
            if name in cookie:
                return cookie[name].value
        except Exception:
            pass
        return None

    def _check_auth(self):
        if not AUTH_ENABLED:
            return True, ADMIN_USER

        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
            user = validate_web_session(token)
            if user:
                return True, user
        elif auth_header.startswith('Basic '):
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode('utf-8')
                u, p = decoded.split(':', 1)
                stored_user, salt, pwd_hash = get_auth_credentials()
                if u == stored_user and verify_password(p, salt, pwd_hash):
                    return True, u
            except Exception:
                pass

        token = self._get_cookie('agy_session')
        user = validate_web_session(token)
        if user:
            return True, user

        return False, None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # Healthcheck endpoints
        if path in ("/health", "/api/health"):
            self._send_json({"status": "ok"})
            return

        # Docker healthcheck fallback: localhost curl to /api/status
        if path == "/api/status" and self.client_address[0] in ("127.0.0.1", "::1", "localhost") and "curl" in self.headers.get("User-Agent", "").lower():
            daemon = get_daemon_status()
            acc = get_active_account()
            profiles = get_profiles_data()
            perms = get_permission_mode()
            self._send_json({
                "daemon": daemon,
                "account": acc,
                "profiles": profiles,
                "permissions": perms
            })
            return

        # Auth status
        if path == "/api/me":
            is_auth, user = self._check_auth()
            self._send_json({"authenticated": is_auth, "user": user if is_auth else None})
            return

        # HTML shell
        if path == "/" or path == "/index.html":
            self._send_html(HTML_TEMPLATE)
            return

        # Protected GET APIs
        is_auth, _ = self._check_auth()
        if not is_auth:
            self._send_json({"error": "Unauthorized", "message": "Vui lòng đăng nhập"}, 401)
            return

        if path == "/api/status":
            daemon = get_daemon_status()
            acc = get_active_account()
            profiles = get_profiles_data()
            perms = get_permission_mode()
            self._send_json({
                "daemon": daemon,
                "account": acc,
                "profiles": profiles,
                "permissions": perms
            })
        elif path == "/api/logs":
            logs = get_daemon_logs(60)
            self._send_text(logs)
        elif path == "/api/usage":
            usage = get_agy_usage()
            self._send_json(usage)
        elif path == "/api/auth/status":
            session_id = qs.get("session_id", [""])[0]
            res = check_oauth_session(session_id)
            self._send_json(res)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8') if length > 0 else ""

        # Login
        if path == "/api/login":
            client_ip = self.client_address[0]
            allowed, err_msg = check_rate_limit(client_ip)
            if not allowed:
                self._send_json({"success": False, "message": err_msg}, 429)
                return
            try:
                data = json.loads(body)
                u = data.get("username", "").strip()
                p = data.get("password", "")
                stored_user, salt, pwd_hash = get_auth_credentials()
                if u == stored_user and verify_password(p, salt, pwd_hash):
                    clear_failed_attempts(client_ip)
                    token = create_web_session(u)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json; charset=utf-8')
                    self.send_header('Set-Cookie', f'agy_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000')
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True, "user": u, "token": token}).encode('utf-8'))
                else:
                    record_failed_attempt(client_ip)
                    self._send_json({"success": False, "message": "Tên đăng nhập hoặc mật khẩu không đúng"}, 401)
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 400)
            return

        # Logout
        if path == "/api/logout":
            token = self._get_cookie('agy_session')
            delete_web_session(token)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Set-Cookie', 'agy_session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode('utf-8'))
            return

        # Protected POST APIs
        is_auth, _ = self._check_auth()
        if not is_auth:
            self._send_json({"error": "Unauthorized", "message": "Vui lòng đăng nhập"}, 401)
            return

        if path == "/api/change-password":
            try:
                data = json.loads(body)
                curr_p = data.get("current_password", "")
                new_p = data.get("new_password", "")
                if not new_p or len(new_p) < 8:
                    self._send_json({"success": False, "message": "Mật khẩu mới phải có tối thiểu 8 ký tự"}, 400)
                    return
                stored_user, salt, pwd_hash = get_auth_credentials()
                if not verify_password(curr_p, salt, pwd_hash):
                    self._send_json({"success": False, "message": "Mật khẩu hiện tại không chính xác"}, 400)
                    return
                update_auth_password(new_p)
                self._send_json({"success": True, "message": "Đổi mật khẩu thành công"})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
        elif path.startswith("/api/daemon/"):
            action = path.split("/")[-1]
            if action in ["start", "stop", "restart"]:
                code, out, err = run_host_cmd(f"systemctl --user {action} {DAEMON_SVC}")
                self._send_json({"success": (code == 0), "message": f"Daemon {action} completed", "output": out or err})
            else:
                self._send_json({"success": False, "message": "Invalid action"}, 400)
        elif path == "/api/permissions/mode" or path == "/api/permissions/toggle":
            try:
                data = json.loads(body)
                mode = data.get("mode")
                if not mode:
                    enable_full = data.get("enable_full", True)
                    mode = "always-proceed" if enable_full else "request-review"
                ok, msg = set_permission_mode(mode)
                self._send_json({"success": ok, "message": msg, "mode": mode})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
        elif path == "/api/auth/start":
            try:
                data = json.loads(body)
                name = data.get("profile_name", "Google Account")
                activate = data.get("activate_now", True)
                sess_id, auth_url, err = start_oauth_session(name, activate)
                self._send_json({
                    "success": bool(auth_url),
                    "session_id": sess_id,
                    "auth_url": auth_url,
                    "error": err if not auth_url else None
                })
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 500)
        elif path == "/api/auth/submit_code":
            try:
                data = json.loads(body)
                sess_id = data.get("session_id")
                code = data.get("code")
                ok, msg = submit_oauth_code(sess_id, code)
                self._send_json({"success": ok, "message": msg})
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 500)
        elif path == "/api/auth/cancel":
            sess_id = qs.get("session_id", [""])[0]
            cancel_oauth_session(sess_id)
            self._send_json({"success": True})
        elif path == "/api/accounts/switch":
            try:
                data = json.loads(body)
                prof_id = data.get("profile_id")
                ok, msg = switch_profile(prof_id)
                self._send_json({"success": ok, "message": msg})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
        elif path == "/api/accounts/delete":
            try:
                data = json.loads(body)
                prof_id = data.get("profile_id")
                ok, msg = delete_profile(prof_id)
                self._send_json({"success": ok, "message": msg})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
        elif path == "/api/accounts/save":
            try:
                data = json.loads(body)
                name = data.get("name", "Profile")
                prof_id = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
                ok = save_current_as_profile(prof_id, name)
                self._send_json({"success": ok, "message": "Saved successfully"})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
        elif path == "/api/accounts/add":
            try:
                data = json.loads(body)
                name = data.get("name")
                token_json = data.get("token_json")
                activate = data.get("activate_now", False)
                ok, msg = add_new_account(name, token_json, activate)
                self._send_json({"success": ok, "message": msg})
            except Exception as e:
                self._send_json({"success": False, "message": str(e)}, 500)
        else:
            self.send_error(404, "Not Found")

def run():
    server = HTTPServer(('0.0.0.0', PORT), RequestHandler)
    print(f"Antigravity Manager running on http://0.0.0.0:{PORT} (Docker: {IS_DOCKER})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()

if __name__ == '__main__':
    run()
