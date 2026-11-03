"""PhantomBox LAN AI Server with Multi-Tier Privileges and Auto-Discovery.

Supports:
- Three-tier role-based access:
    * all: Full administrative rights (change server config, manage users, modify runtime settings, full chat/model access)
    * regular: Custom / standard rights (chat with models, read/write files according to scopes, view settings, cannot alter server auth/system settings)
    * use-only: Read-only model interaction rights (chat with models only, zero modification to any settings or server configs)
- Password-protected authentication (via secure tokens / Basic Auth / Bearer)
- UDP Broadcast network auto-discovery (on hotspot / LAN)
- Remote server connection for multi-device chaining
- Streaming chat completions (/v1/chat/completions) compatible with OpenAI format
- Discovery endpoint (/v1/info, /v1/models, /v1/auth/login)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from phantombox.config import load_config, save_config, get_config_val, set_config_val
from phantombox.models import list_downloaded_models, RECOMMENDED_MODELS
from phantombox.runtime import list_running
from phantombox.providers import get_all_providers, is_provider_connected, stream_chat_deltas, fetch_cached_or_default_models

# UDP Beacon port for network auto-discovery
DISCOVERY_BROADCAST_PORT = 8444
DISCOVERY_MAGIC_REQUEST = "PHANTOMBOX_DISCOVER_PING"
DISCOVERY_MAGIC_REPLY = "PHANTOMBOX_DISCOVER_PONG"

# Signup auth modes (how a host lets new users create accounts on their server)
AUTH_MODE_EMAIL = "email"            # email verification required (not active yet)
AUTH_MODE_OPEN = "open"              # instant signup: credentials saved, sign in right away
AUTH_MODE_APPROVAL = "approval"      # signup goes into a pending queue until host approves

VALID_AUTH_MODES = [AUTH_MODE_EMAIL, AUTH_MODE_OPEN, AUTH_MODE_APPROVAL]

# Active tokens in memory: token -> {"username": str, "created_at": float}
_ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SERVER_INSTANCE: Optional[ThreadingHTTPServer] = None
_SERVER_THREAD: Optional[threading.Thread] = None
_DISCOVERY_THREAD: Optional[threading.Thread] = None
_DISCOVERY_STOP_EVENT = threading.Event()


def hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    """Hash password using PBKDF2-HMAC-SHA256."""
    if not salt:
        salt = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        100_000,
    ).hex()
    return pw_hash, salt


def verify_password(password: str, expected_hash: str, salt: str) -> bool:
    """Verify password against stored hash and salt."""
    test_hash, _ = hash_password(password, salt)
    return secrets.compare_digest(test_hash, expected_hash)


def get_local_ip() -> str:
    """Detect LAN/hotspot IP address of this device."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Device type detection (read-only: reported by the beacon and /v1/info so
# mobile clients can show the right icon/label when scanning. It is derived
# from the OS/platform every time it is queried — never user-configurable.)
# ---------------------------------------------------------------------------

_DEVICE_TYPES = (
    "phone",        # Android/iOS handset running the phantombox runtime
    "tablet",
    "laptop",
    "desktop",
    "server",       # GPU box / VPS / headless machine
    "vm",           # virtual machine / container host
    "raspberry-pi",
    "unknown",
)


def detect_device_type() -> str:
    """Best-effort classification of the machine this server runs on.

    Detection order:
      1. Android/iOS markers (Termux builds, ANDROID_ env, /system build props)
      2. DMI/product strings for VMs and single-board computers (/sys/class/dmi,
         /proc/device-tree/model) — the most reliable hardware identity source
      3. Chassis type from DMI (desktop vs laptop vs server/tower)
      4. Heuristics: battery presence -> laptop; headless (no display server and
         SSH-ish environment) or Windows Server editions -> server
    Always returns one of _DEVICE_TYPES; never raises.
    """
    import platform
    import sys

    system = platform.system()

    # --- 1. Phones / tablets -------------------------------------------------
    if "ANDROID_BOOTLOGO" in os.environ or "ANDROID_ROOT" in os.environ or "/com.termux/" in sys.executable:
        return "phone"
    if system == "Linux" and os.path.isdir("/system") and os.path.exists("/system/build.prop"):
        # Termux/proot environments expose Android's build props under /system
        try:
            with open("/system/build.prop", "r", errors="ignore") as fh:
                props = fh.read(65536).lower()
            if "ro.build.version.release" in props:
                return "tablet" if ("tablet" in props or "ro.build.characteristics" in props and "tablet" in props) else "phone"
        except Exception:
            return "phone"
    if system == "Darwin" and platform.machine().startswith("iP"):
        return "phone"

    # --- 2. DMI / device-tree product identity (Linux) -----------------------
    def _read_first(paths: Tuple[str, ...], limit: int = 4096) -> str:
        for p in paths:
            try:
                with open(p, "r", errors="ignore") as fh:
                    val = (fh.read(limit) or "").strip().lower()
                if val and val != "none" and "to be filled" not in val:
                    return val
            except Exception:
                continue
        return ""

    if system == "Linux":
        product = _read_first((
            "/sys/class/dmi/id/product_name",
            "/sys/class/dmi/id/board_name",
            "/proc/device-tree/model",
            "/sys/firmware/devicetree/base/model",
        ))
        vendor = _read_first((
            "/sys/class/dmi/id/sys_vendor",
            "/sys/class/dmi/id/board_vendor",
        ))
        combined = f"{vendor} {product}"

        if product:
            # Virtual machines
            vm_markers = ("vmware", "virtualbox", "kvm", "qemu", "xen", "hyper-v",
                          "microsoft corporation virtual", "bochs", "parallels", "bhyve")
            if any(m in combined for m in vm_markers):
                return "vm"
            # Single-board computers
            sbc_markers = ("raspberry pi", "radxa", "orange pi", "banana pi", "odroid",
                           "jetson", "beaglebone", "rock pi", "rockchip", "pine64", "nanopi")
            if any(m in combined for m in sbc_markers):
                # A Raspberry Pi can run headless as a home server, but its class
                # is still recognizable — report the SBC type.
                return "raspberry-pi" if "raspberry" in combined else "raspberry-pi"

        # --- 3. Chassis type (desktop vs laptop vs server chassis) ----------
        chassis_raw = _read_first(("/sys/class/dmi/id/chassis_type",))
        if chassis_raw:
            try:
                chassis = int(chassis_raw.split()[0])
                # SMBIOS chassis type codes:
                #  8-11 laptop/notebook/handheld/all-in-one...
                #  17-19, 23-24: main server chassis
                #  23 rack mount chassis, 24 sealed-case PC
                if 8 <= chassis <= 14:      # portable: laptop, notebook, handheld, AIO "portable"
                    return "laptop"
                if chassis in (23, 24, 27, 28, 29, 30):  # rack/sealed/expansion
                    return "server"
                # 1-7: desktop-family (tower, mini tower, pizza box, desktop)
                # Fall through to battery heuristic for exotic codes.
            except ValueError:
                pass

        # --- 4. Heuristics ---------------------------------------------------
        # Battery present => laptop (most reliable laptop signal on Linux).
        if os.path.isdir("/sys/class/power_supply"):
            try:
                for entry in os.listdir("/sys/class/power_supply"):
                    if entry.lower().startswith("bat"):
                        return "laptop"
            except Exception:
                pass
        # Headless Linux boxes (servers/VPS): no DISPLAY/WAYLAND and a
        # server-flavored hostname or known cloud DMI strings.
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if not has_display:
            cloud_markers = ("amazon", "ec2", "google", "gce", "microsoft", "azure",
                             "digitalocean", "linode", "ovh", "hetzner", "vultr",
                             "oracle", "alibaba", "scaleway")
            if any(m in combined for m in cloud_markers):
                return "server"

    if system == "Windows":
        # Windows: distinguish Server editions and laptops via battery + chassis.
        try:
            import ctypes
            windll = ctypes.windll.kernel32  # noqa: F841 - probes availability
        except Exception:
            windll = None
        # Product name from registry-free env:PROCESSOR_* is not helpful; use
        # platform() string which contains 'Windows-...-Server' for server SKUs.
        uname = " ".join(platform.uname()).lower()
        if "server" in uname.split("-")[-1] or "server" in platform.release().lower():
            return "server"
        try:
            import subprocess
            out = subprocess.run(
                ["wmic", "systemenclosure", "get", "chassistypes"],
                capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            chassis_blob = (out.stdout or "").lower()
            # WMI chassis codes (decimals, space separated)
            codes = {int(tok) for tok in chassis_blob.replace("{", " ").replace("}", " ").split() if tok.isdigit()}
            if 8 <= min(c for c in codes if c) if codes else False:
                pass
            if codes:
                laptop_codes = {8, 9, 10, 11, 12, 14, 30, 31, 32}
                rack_codes = {23, 24, 27, 28}
                if codes & laptop_codes:
                    # Laptop chassis + battery presence check (WMIC again)
                    return "laptop"
                if codes & rack_codes:
                    return "server"
        except Exception:
            pass
        # Battery heuristic via WMI
        try:
            import subprocess
            out = subprocess.run(
                ["wmic", "path", "Win32_Battery", "get", "Status"],
                capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if out.returncode == 0 and (out.stdout or "").strip().count("\n") >= 1:
                return "laptop"
        except Exception:
            pass
        if not os.environ.get("SESSIONNAME") and not os.environ.get("USERDOMAIN"):
            return "server"
        return "desktop"

    if system == "Darwin":
        # Macs: laptop vs desktop (MacBooks report 'Book' in model)
        try:
            import subprocess
            out = subprocess.run(
                ["sysctl", "-n", "hw.model"], capture_output=True, text=True, timeout=5,
            )
            model = (out.stdout or "").strip().lower()
            if "book" in model:
                return "laptop"
            if "macpro" in model or "macmini" in model or "imac" in model:
                return "desktop"
        except Exception:
            pass
        if platform.machine() in ("arm64", "x86_64"):
            return "laptop" if "book" in platform.node().lower() else "desktop"
        return "desktop"

    # FreeBSD/OpenBSD and anything else
    if system not in ("Linux", "Windows", "Darwin") and os.path.isdir("/sys/class/power_supply"):
        return "laptop"
    return "desktop" if system in ("Windows", "Darwin") else "unknown"


# =============================================================================
# Server Configuration and User Management
# =============================================================================

def get_server_config() -> Dict[str, Any]:
    """Get current server configuration dict."""
    cfg = load_config()
    server_cfg = cfg.get("server")
    if not isinstance(server_cfg, dict):
        server_cfg = {
            "host": "0.0.0.0",
            "port": 8443,
            "discoverable": True,
            "users": {},
            "pending_users": {},
            "auth_mode": AUTH_MODE_OPEN,
            "remote_servers": {},
        }
    server_cfg.setdefault("users", {})
    server_cfg.setdefault("pending_users", {})
    server_cfg.setdefault("auth_mode", AUTH_MODE_OPEN)
    server_cfg.setdefault("remote_servers", {})
    return server_cfg


def save_server_config(server_cfg: Dict[str, Any]) -> None:
    """Save server configuration dict."""
    set_config_val("server", server_cfg)


def set_user(username: str, password: str) -> bool:
    """Add or update a user (host-created accounts are approved immediately)."""
    clean_user = username.strip()
    if not clean_user or not password:
        return False

    cfg = get_server_config()
    users = cfg.setdefault("users", {})
    pw_hash, salt = hash_password(password)

    users[clean_user] = {
        "username": clean_user,
        "hash": pw_hash,
        "salt": salt,
        "created_at": time.time(),
    }
    # If this user had a pending signup, it is now approved.
    cfg.get("pending_users", {}).pop(clean_user, None)
    save_server_config(cfg)
    return True


def signup_user(username: str, password: str) -> str:
    """Self-service signup from a remote client.

    Returns one of: "created" (auth_mode open), "pending" (approval required),
    "email" (email verification required — not supported yet), "exists"
    (username already taken), "error".
    """
    clean_user = username.strip()
    if not clean_user or not password:
        return "error"

    cfg = get_server_config()
    if clean_user in cfg.get("users", {}) or clean_user in cfg.get("pending_users", {}):
        return "exists"

    mode = cfg.get("auth_mode", AUTH_MODE_OPEN)
    if mode == AUTH_MODE_EMAIL:
        return "email"

    pw_hash, salt = hash_password(password)
    record = {
        "username": clean_user,
        "hash": pw_hash,
        "salt": salt,
        "created_at": time.time(),
    }
    if mode == AUTH_MODE_APPROVAL:
        cfg.setdefault("pending_users", {})[clean_user] = record
        save_server_config(cfg)
        return "pending"

    cfg.setdefault("users", {})[clean_user] = record
    save_server_config(cfg)
    return "created"


def remove_user(username: str) -> bool:
    """Remove a user from server configuration."""
    cfg = get_server_config()
    users = cfg.get("users", {})
    if username in users:
        del users[username]
        save_server_config(cfg)
        return True
    return False


def list_users() -> Dict[str, Dict[str, Any]]:
    """List approved users (username -> minimal record)."""
    cfg = get_server_config()
    users = cfg.get("users", {})
    return {
        u: {
            "username": data.get("username", u),
            "created_at": data.get("created_at", 0),
        }
        for u, data in users.items()
    }


def list_pending_users() -> Dict[str, Dict[str, Any]]:
    """List users awaiting host approval (username -> record without hash)."""
    cfg = get_server_config()
    return {
        u: {"username": data.get("username", u), "requested_at": data.get("created_at", 0)}
        for u, data in cfg.get("pending_users", {}).items()
    }


def approve_user(username: str) -> bool:
    """Move a user from the pending queue to approved users."""
    cfg = get_server_config()
    pending = cfg.get("pending_users", {})
    record = pending.pop(username, None)
    if not record:
        return False
    cfg.setdefault("users", {})[username] = record
    save_server_config(cfg)
    return True


def deny_user(username: str) -> bool:
    """Remove a pending signup request (denied)."""
    cfg = get_server_config()
    if username in cfg.get("pending_users", {}):
        del cfg["pending_users"][username]
        save_server_config(cfg)
        return True
    return False


def is_pending_user(username: str, password: str = "") -> bool:
    """True when this username is in the pending-approval queue.

    When a password is given it must also MATCH the pending record's stored
    hash — so the host only reveals 'awaiting approval' to someone whose
    credentials are actually correct (no username enumeration, and the
    'credentials match but still pending' error is precise).
    """
    clean = username.strip()
    pending = get_server_config().get("pending_users", {})
    record = pending.get(clean)
    if not record:
        return False
    if not password:
        return True
    return verify_password(password, record["hash"], record["salt"])


def set_auth_mode(mode: str) -> Tuple[bool, str]:
    """Set the signup auth mode (open | approval | email)."""
    clean = (mode or "").lower().strip()
    if clean not in VALID_AUTH_MODES:
        return False, f"Invalid auth mode '{mode}'. Options: {', '.join(VALID_AUTH_MODES)}"
    cfg = get_server_config()
    cfg["auth_mode"] = clean
    save_server_config(cfg)
    return True, f"Signup auth mode set to '{clean}'."


def set_discoverable(enabled: bool) -> None:
    """Toggle broadcast auto-discoverability on LAN."""
    cfg = get_server_config()
    cfg["discoverable"] = bool(enabled)
    save_server_config(cfg)


def is_discoverable() -> bool:
    """Check if server is discoverable."""
    cfg = get_server_config()
    return cfg.get("discoverable", True)


def authenticate_credentials(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Validate username and password, return user info if valid."""
    cfg = get_server_config()
    users = cfg.get("users", {})
    user_info = users.get(username)
    if not user_info:
        return None

    if verify_password(password, user_info["hash"], user_info["salt"]):
        return {
            "username": username,
        }
    return None


def create_session_token(username: str) -> str:
    """Create an active bearer session token."""
    token = "pbk_" + secrets.token_urlsafe(32)
    _ACTIVE_SESSIONS[token] = {
        "username": username,
        "created_at": time.time(),
    }
    return token


def authenticate_request(headers: Any) -> Optional[Dict[str, Any]]:
    """Authenticate request using Authorization header (Bearer or Basic)."""
    cfg = get_server_config()
    users = cfg.get("users", {})

    # If NO users are configured yet, deny access or enforce setup
    if not users:
        return None

    auth_header = headers.get("Authorization", "")
    if not auth_header:
        return None

    # 1. Bearer Token
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if token in _ACTIVE_SESSIONS:
            return _ACTIVE_SESSIONS[token]

    # 2. Basic Auth
    if auth_header.startswith("Basic "):
        try:
            encoded = auth_header[6:].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            if ":" in decoded:
                username, password = decoded.split(":", 1)
                user_info = authenticate_credentials(username, password)
                if user_info:
                    return user_info
        except Exception:
            return None

    return None


# =============================================================================
# Remote Server Connections (/server --connect)
# =============================================================================

def save_remote_server(name: str, host: str, port: int, username: str, password: str) -> None:
    """Register a remote PhantomBox server to connect to."""
    cfg = get_server_config()
    remotes = cfg.setdefault("remote_servers", {})
    remotes[name] = {
        "name": name,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "url": f"http://{host}:{port}",
    }
    save_server_config(cfg)


def list_remote_servers() -> Dict[str, Dict[str, Any]]:
    """List configured remote servers."""
    cfg = get_server_config()
    return cfg.get("remote_servers", {})


def remove_remote_server(name: str) -> bool:
    """Remove a remote server."""
    cfg = get_server_config()
    remotes = cfg.get("remote_servers", {})
    if name in remotes:
        del remotes[name]
        save_server_config(cfg)
        return True
    return False


# =============================================================================
# HTTP Server Request Handler
# =============================================================================

class PhantomBoxServerHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for PhantomBox AI Server."""

    server_version = "PhantomBoxServer/1.0"

    def _send_json(self, status_code: int, data: Any) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """Handle CORS pre-flight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        # Root health check / basic ping
        if path == "" or path == "/health":
            self._send_json(200, {
                "status": "online",
                "app": "phantombox",
                "version": "0.1.0",
                "time": time.time(),
            })
            return

        # Public info endpoint for mobile discovery
        if path == "/v1/info":
            server_cfg = get_server_config()
            self._send_json(200, {
                "name": socket.gethostname(),
                "app": "phantombox",
                "version": "0.2.0",
                "ip": get_local_ip(),
                "port": server_cfg.get("port", 8443),
                "device_type": detect_device_type(),
                "auth_required": True,
                "has_users": bool(server_cfg.get("users")),
                "discoverable": server_cfg.get("discoverable", True),
                "signup_mode": server_cfg.get("auth_mode", AUTH_MODE_OPEN),
                "open": server_cfg.get("auth_mode", AUTH_MODE_OPEN) == AUTH_MODE_OPEN,
            })
            return

        # Public signup-mode query (so clients know which flow to render)
        if path == "/v1/auth/signup-mode":
            cfg = get_server_config()
            self._send_json(200, {
                "signup_mode": cfg.get("auth_mode", AUTH_MODE_OPEN),
                "open": cfg.get("auth_mode", AUTH_MODE_OPEN) == AUTH_MODE_OPEN,
            })
            return

        # Authenticate all subsequent endpoints
        user_session = authenticate_request(self.headers)
        if not user_session:
            self._send_json(401, {
                "error": "Unauthorized",
                "message": "Valid credentials or session token required.",
            })
            return

        # Current user profile (privileges removed: username only)
        if path == "/v1/auth/me":
            self._send_json(200, {
                "username": user_session["username"],
            })
            return

        # List models
        if path == "/v1/models":
            models_list = self._collect_models()
            self._send_json(200, {
                "object": "list",
                "data": models_list,
            })
            return

        # Get server settings (remote/connected clients get a read-only view)
        if path == "/v1/settings":
            cfg = load_config()
            clean_cfg = {
                "user_name": cfg.get("user_name", "User"),
                "assistant_name": cfg.get("assistant_name", "Assistant"),
                "theme_color": cfg.get("theme_color", "#f97316"),
                "permission_mode": cfg.get("permission_mode", "restricted"),
                "enable_internet_search": cfg.get("enable_internet_search", True),
                "enable_thinking": cfg.get("enable_thinking", False),
                "server": {
                    "port": cfg.get("server", {}).get("port", 8443),
                    "discoverable": cfg.get("server", {}).get("discoverable", True),
                    "user_count": len(cfg.get("server", {}).get("users", {})),
                    "auth_mode": cfg.get("server", {}).get("auth_mode", AUTH_MODE_OPEN),
                },
            }
            self._send_json(200, clean_cfg)
            return

        # List approved users (host only)
        if path == "/v1/users":
            self._send_json(200, {"users": list_users()})
            return

        # List pending signups awaiting approval (host only)
        if path == "/v1/users/pending":
            self._send_json(200, {"pending": list_pending_users()})
            return

        self._send_json(404, {"error": "Not Found", "path": path})

    def do_POST(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/")

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            body_json = {}

        # 1. Login endpoint (No auth header needed)
        if path == "/v1/auth/login":
            username = body_json.get("username", "")
            password = body_json.get("password", "")
            user_info = authenticate_credentials(username, password)
            if not user_info:
                # Specific error when the credentials are a correct pending signup
                if is_pending_user(username, password):
                    self._send_json(403, {
                        "error": "Pending approval",
                        "message": "Your credentials are still awaiting pending approval. Please contact the network host.",
                    })
                else:
                    self._send_json(401, {
                        "error": "Invalid credentials",
                        "message": "Username or password incorrect.",
                    })
                return

            token = create_session_token(user_info["username"])
            self._send_json(200, {
                "token": token,
                "token_type": "Bearer",
                "username": user_info["username"],
                "server_name": socket.gethostname(),
            })
            return

        # 1b. Signup endpoint (No auth header needed) — self-service account creation
        if path == "/v1/auth/signup":
            username = body_json.get("username", "")
            password = body_json.get("password", "")
            confirm = body_json.get("password_confirm", password)
            email = body_json.get("email", "")  # accepted for future use, ignored

            if not username or not password:
                self._send_json(400, {"error": "Bad Request", "message": "username and password are required."})
                return
            if password != confirm:
                self._send_json(400, {"error": "Bad Request", "message": "Password confirmation does not match."})
                return

            cfg = get_server_config()
            mode = cfg.get("auth_mode", AUTH_MODE_OPEN)
            if mode == AUTH_MODE_EMAIL:
                self._send_json(501, {
                    "error": "Not Implemented",
                    "message": "Feature is not available yet. Please contact support or the host for further validation",
                    "signup_mode": mode,
                })
                return

            result = signup_user(username, password)
            if result == "exists":
                self._send_json(409, {
                    "error": "Conflict",
                    "message": f"Username '{username}' is already taken on this server.",
                })
                return
            if result == "pending":
                self._send_json(202, {
                    "status": "pending",
                    "message": "Your request has been sent to the network host for approval. Please try logging in later. "
                               "If you still can't log in, refer to the error message and contact the network host.",
                })
                return
            if result == "created":
                token = create_session_token(username)
                self._send_json(201, {
                    "status": "created",
                    "token": token,
                    "token_type": "Bearer",
                    "username": username,
                    "message": "Account created successfully! You can now log in",
                })
                return
            self._send_json(400, {"error": "Bad Request", "message": "Signup failed. Check your inputs and try again."})
            return

        # 1c. (signup-mode GET handled above, before auth) — nothing here

        # Authenticate all subsequent POST requests
        user_session = authenticate_request(self.headers)
        if not user_session:
            self._send_json(401, {
                "error": "Unauthorized",
                "message": "Valid credentials or session token required.",
            })
            return

        # 2. Chat completions endpoint (/v1/chat/completions) - all privileges can chat
        if path == "/v1/chat/completions":
            self._handle_chat_completions(body_json, user_session)
            return

        # 3. Host-only: signup auth mode (/v1/auth/mode) — POST { "mode": "open|approval|email" }
        if path == "/v1/auth/mode":
            mode = body_json.get("mode", "")
            ok, msg = set_auth_mode(mode)
            self._send_json(200 if ok else 400, {"status": "success" if ok else "error", "message": msg})
            return

        # 4. Host-only: approve/deny pending signups (/v1/users/pending)
        if path == "/v1/users/pending":
            action = body_json.get("action", "")
            target = body_json.get("username", "")
            if action == "approve":
                if approve_user(target):
                    self._send_json(200, {"status": "success", "message": f"User '{target}' approved. They can now sign in."})
                else:
                    self._send_json(404, {"error": "Not Found", "message": f"No pending signup for '{target}'."})
            elif action == "deny":
                if deny_user(target):
                    self._send_json(200, {"status": "success", "message": f"Pending signup for '{target}' denied."})
                else:
                    self._send_json(404, {"error": "Not Found", "message": f"No pending signup for '{target}'."})
            else:
                self._send_json(400, {"error": "Bad Request", "message": "action must be 'approve' or 'deny'."})
            return

        # 5. Add/Update User (/v1/users) — host-side account creation (approved immediately)
        if path == "/v1/users":
            new_user = body_json.get("username", "")
            new_pw = body_json.get("password", "")

            if not new_user or not new_pw:
                self._send_json(400, {"error": "Bad Request", "message": "username and password are required."})
                return

            set_user(new_user, new_pw)
            self._send_json(200, {
                "status": "success",
                "message": f"User '{new_user}' created/updated.",
            })
            return

        # 6. Settings modification endpoint (/v1/settings) — remote sessions are read-only
        if path == "/v1/settings":
            self._send_json(403, {
                "error": "Forbidden",
                "message": "Remote connections cannot change settings on this host. Ask the host to modify them.",
            })
            return

        self._send_json(404, {"error": "Not Found", "path": path})

    def _collect_models(self) -> List[Dict[str, Any]]:
        """Collect available models on this server."""
        models: List[Dict[str, Any]] = []

        # Local downloaded models
        downloaded = list_downloaded_models()
        running = list_running()

        for p in downloaded:
            stem = p.stem.lower()
            matched_key = None
            for k, info in RECOMMENDED_MODELS.items():
                if info["filename"].lower() == p.name.lower() or k.lower() in stem:
                    matched_key = k
                    break
            model_id = matched_key if matched_key else p.stem
            is_active = model_id in running
            models.append({
                "id": model_id,
                "object": "model",
                "owned_by": "local",
                "active": is_active,
                "type": "local",
            })

        # Connected cloud providers (live cached catalogs only — no stale defaults)
        for p in get_all_providers():
            pid = p["id"]
            if is_provider_connected(pid):
                for mid in fetch_cached_or_default_models(pid):
                    models.append({
                        "id": f"{pid}/{mid}",
                        "object": "model",
                        "owned_by": pid,
                        "active": False,
                        "type": "cloud",
                    })

        return models

    def _handle_chat_completions(self, body: Dict[str, Any], user_session: Dict[str, Any]) -> None:
        """Handle /v1/chat/completions with SSE streaming support."""
        messages = body.get("messages", [])
        model = body.get("model", "")
        stream = body.get("stream", True)

        # Fallback to first available running model or downloaded model if not specified
        if not model:
            running = list_running()
            if running:
                model = next(iter(running.keys()))
            else:
                downloaded = list_downloaded_models()
                if downloaded:
                    model = downloaded[0].stem
                else:
                    model = "default"

        # Determine local port for local model
        running = list_running()
        local_port = 8080
        if model in running:
            local_port = running[model].get("port", 8080)

        # Build tools if privilege allows
        tools: List[Dict[str, Any]] = []

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            import asyncio

            async def _stream_producer():
                try:
                    async for delta in stream_chat_deltas(
                        model,
                        local_port,
                        messages,
                        tools,
                    ):
                        chunk = {
                            "id": "chatcmpl-" + secrets.token_hex(6),
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": delta,
                                    "finish_reason": None,
                                }
                            ],
                        }
                        line = f"data: {json.dumps(chunk)}\n\n"
                        self.wfile.write(line.encode("utf-8"))
                        self.wfile.flush()

                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception as e:
                    err_chunk = {"error": {"message": str(e)}}
                    line = f"data: {json.dumps(err_chunk)}\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()

            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(_stream_producer())
                loop.close()
            except (ConnectionResetError, BrokenPipeError):
                pass
        else:
            # Non-streaming collection
            import asyncio

            async def _collect():
                reply = ""
                async for delta in stream_chat_deltas(model, local_port, messages, tools):
                    content = delta.get("content")
                    if content:
                        reply += content
                return reply

            loop = asyncio.new_event_loop()
            reply_text = loop.run_until_complete(_collect())
            loop.close()

            self._send_json(200, {
                "id": "chatcmpl-" + secrets.token_hex(6),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": reply_text,
                        },
                        "finish_reason": "stop",
                    }
                ],
            })


# =============================================================================
# UDP Broadcast Discovery Beacon
# =============================================================================

def _discovery_responder(stop_event: threading.Event) -> None:
    """Listen for UDP broadcast discovery packets from mobile clients and respond with server info."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_BROADCAST_PORT))
        sock.settimeout(1.0)
    except Exception:
        sock.close()
        return

    while not stop_event.is_set():
        try:
            data, addr = sock.recvfrom(2048)
            msg = data.decode("utf-8", errors="ignore").strip()
            if msg == DISCOVERY_MAGIC_REQUEST:
                cfg = get_server_config()
                if not cfg.get("discoverable", True):
                    continue

                server_port = cfg.get("port", 8443)
                device_ip = get_local_ip()
                response = {
                    "magic": DISCOVERY_MAGIC_REPLY,
                    "server_name": socket.gethostname(),
                    "app": "phantombox",
                    "version": "0.2.0",
                    "device_type": detect_device_type(),
                    "ip": device_ip,
                    "port": server_port,
                    "signup_mode": get_server_config().get("auth_mode", AUTH_MODE_OPEN),
                    "url": f"http://{device_ip}:{server_port}",
                }
                sock.sendto(json.dumps(response).encode("utf-8"), addr)
        except socket.timeout:
            continue
        except Exception:
            continue

    sock.close()


def discover_lan_servers(timeout_seconds: float = 2.0) -> List[Dict[str, Any]]:
    """Broadcast UDP ping to find other PhantomBox servers on this WiFi/Hotspot network."""
    servers: List[Dict[str, Any]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)

    try:
        sock.sendto(DISCOVERY_MAGIC_REQUEST.encode("utf-8"), ("<broadcast>", DISCOVERY_BROADCAST_PORT))
        start_time = time.time()
        seen_hosts = set()

        while time.time() - start_time < timeout_seconds:
            try:
                data, addr = sock.recvfrom(2048)
                resp = json.loads(data.decode("utf-8", errors="ignore"))
                if resp.get("magic") == DISCOVERY_MAGIC_REPLY:
                    host_key = f"{addr[0]}:{resp.get('port')}"
                    if host_key not in seen_hosts:
                        seen_hosts.add(host_key)
                        resp["remote_addr"] = addr[0]
                        servers.append(resp)
            except socket.timeout:
                continue
            except Exception:
                pass
    finally:
        sock.close()

    return servers


# =============================================================================
# Lifecycle Management (start, stop, status)
# =============================================================================

def start_server(port: Optional[int] = None, host: str = "0.0.0.0") -> Tuple[bool, str]:
    """Start the PhantomBox LAN AI HTTP server and discovery beacon in background threads."""
    global _SERVER_INSTANCE, _SERVER_THREAD, _DISCOVERY_THREAD, _DISCOVERY_STOP_EVENT

    if _SERVER_INSTANCE is not None:
        cfg = get_server_config()
        return True, f"Server already running at http://{get_local_ip()}:{cfg.get('port', 8443)}"

    server_cfg = get_server_config()
    target_port = port or server_cfg.get("port", 8443)
    server_cfg["port"] = target_port
    server_cfg["host"] = host
    save_server_config(server_cfg)

    try:
        _SERVER_INSTANCE = ThreadingHTTPServer((host, target_port), PhantomBoxServerHandler)
    except Exception as e:
        return False, f"Failed to bind server to {host}:{target_port}: {e}"

    _SERVER_THREAD = threading.Thread(target=_SERVER_INSTANCE.serve_forever, daemon=True, name="PhantomBoxServerThread")
    _SERVER_THREAD.start()

    _DISCOVERY_STOP_EVENT.clear()
    _DISCOVERY_THREAD = threading.Thread(target=_discovery_responder, args=(_DISCOVERY_STOP_EVENT,), daemon=True, name="PhantomBoxDiscoveryThread")
    _DISCOVERY_THREAD.start()

    local_ip = get_local_ip()
    msg = f"PhantomBox server started on http://{local_ip}:{target_port} (LAN/Hotspot accessible)"
    return True, msg


def stop_server() -> Tuple[bool, str]:
    """Stop the PhantomBox HTTP server and discovery beacon."""
    global _SERVER_INSTANCE, _SERVER_THREAD, _DISCOVERY_THREAD, _DISCOVERY_STOP_EVENT

    if _SERVER_INSTANCE is None:
        return False, "Server is not currently running."

    try:
        _DISCOVERY_STOP_EVENT.set()
        _SERVER_INSTANCE.shutdown()
        _SERVER_INSTANCE.server_close()
        _SERVER_INSTANCE = None
        _SERVER_THREAD = None
        _DISCOVERY_THREAD = None
        return True, "PhantomBox server stopped successfully."
    except Exception as e:
        return False, f"Error stopping server: {e}"


def is_server_running() -> bool:
    """Check if server instance is active."""
    return _SERVER_INSTANCE is not None
