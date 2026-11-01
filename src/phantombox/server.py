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

# Privilege levels
PRIVILEGE_ALL = "all"
PRIVILEGE_REGULAR = "regular"
PRIVILEGE_USE_ONLY = "use-only"

VALID_PRIVILEGES = [PRIVILEGE_ALL, PRIVILEGE_REGULAR, PRIVILEGE_USE_ONLY]

# Active tokens in memory: token -> {"username": str, "privilege": str, "created_at": float}
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
            "remote_servers": {},
        }
    return server_cfg


def save_server_config(server_cfg: Dict[str, Any]) -> None:
    """Save server configuration dict."""
    set_config_val("server", server_cfg)


def set_user(username: str, password: str, privilege: str = PRIVILEGE_REGULAR) -> bool:
    """Add or update a user with a privilege level."""
    clean_user = username.strip()
    if not clean_user or not password:
        return False

    clean_priv = privilege.lower().strip()
    if clean_priv not in VALID_PRIVILEGES:
        clean_priv = PRIVILEGE_REGULAR

    cfg = get_server_config()
    users = cfg.setdefault("users", {})
    pw_hash, salt = hash_password(password)

    users[clean_user] = {
        "username": clean_user,
        "hash": pw_hash,
        "salt": salt,
        "privilege": clean_priv,
        "updated_at": time.time(),
    }
    save_server_config(cfg)
    return True


def remove_user(username: str) -> bool:
    """Remove a user from server configuration."""
    cfg = get_server_config()
    users = cfg.get("users", {})
    if username in users:
        del users[username]
        save_server_config(cfg)
        return True
    return False


def list_users() -> Dict[str, str]:
    """List configured users and their privilege levels."""
    cfg = get_server_config()
    users = cfg.get("users", {})
    return {u: data.get("privilege", PRIVILEGE_REGULAR) for u, data in users.items()}


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
            "privilege": user_info.get("privilege", PRIVILEGE_REGULAR),
        }
    return None


def create_session_token(username: str, privilege: str) -> str:
    """Create an active bearer session token."""
    token = "pbk_" + secrets.token_urlsafe(32)
    _ACTIVE_SESSIONS[token] = {
        "username": username,
        "privilege": privilege,
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
                "version": "0.1.0",
                "ip": get_local_ip(),
                "port": server_cfg.get("port", 8443),
                "auth_required": True,
                "has_users": bool(server_cfg.get("users")),
                "discoverable": server_cfg.get("discoverable", True),
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

        # Current user profile and privilege
        if path == "/v1/auth/me":
            self._send_json(200, {
                "username": user_session["username"],
                "privilege": user_session["privilege"],
            })
            return

        # List models (all privileges allowed)
        if path == "/v1/models":
            models_list = self._collect_models()
            self._send_json(200, {
                "object": "list",
                "data": models_list,
            })
            return

        # Get server settings (all and regular privileges allowed to view; use-only allowed to view current settings)
        if path == "/v1/settings":
            cfg = load_config()
            # Omit sensitive keys like passwords and secret API tokens if privilege is use-only
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
                },
            }
            if user_session["privilege"] == PRIVILEGE_ALL:
                clean_cfg["users"] = list_users()
            self._send_json(200, clean_cfg)
            return

        # List users (only privilege: 'all')
        if path == "/v1/users":
            if user_session["privilege"] != PRIVILEGE_ALL:
                self._send_json(403, {"error": "Forbidden", "message": "Only 'all' privilege users can manage users."})
                return
            self._send_json(200, {"users": list_users()})
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
                self._send_json(401, {
                    "error": "Invalid credentials",
                    "message": "Username or password incorrect.",
                })
                return

            token = create_session_token(user_info["username"], user_info["privilege"])
            self._send_json(200, {
                "token": token,
                "token_type": "Bearer",
                "username": user_info["username"],
                "privilege": user_info["privilege"],
                "server_name": socket.gethostname(),
            })
            return

        # Authenticate all subsequent POST requests
        user_session = authenticate_request(self.headers)
        if not user_session:
            self._send_json(401, {
                "error": "Unauthorized",
                "message": "Valid credentials or session token required.",
            })
            return

        user_privilege = user_session["privilege"]

        # 2. Chat completions endpoint (/v1/chat/completions) - all privileges can chat
        if path == "/v1/chat/completions":
            self._handle_chat_completions(body_json, user_session)
            return

        # 3. Settings modification endpoint (/v1/settings)
        if path == "/v1/settings":
            # Privilege check:
            # - 'use-only': CANNOT change any settings (403 Forbidden)
            # - 'regular': Custom privileges (can change UI session/display preferences like assistant name, theme, but NOT server auth or core runtime privileges)
            # - 'all': Can change any setting
            if user_privilege == PRIVILEGE_USE_ONLY:
                self._send_json(403, {
                    "error": "Forbidden",
                    "message": "User privilege 'use-only' is restricted from changing any settings.",
                })
                return

            if user_privilege == PRIVILEGE_REGULAR:
                # Regular users can only change non-server settings
                allowed_regular_keys = {"assistant_name", "user_name", "theme_color", "enable_thinking", "enable_internet_search"}
                disallowed = set(body_json.keys()) - allowed_regular_keys
                if disallowed:
                    self._send_json(403, {
                        "error": "Forbidden",
                        "message": f"Regular privilege cannot modify: {list(disallowed)}. 'all' privilege required.",
                    })
                    return

            # Apply settings changes
            for k, v in body_json.items():
                if k == "discoverable" and user_privilege == PRIVILEGE_ALL:
                    set_discoverable(bool(v))
                elif k != "server":
                    set_config_val(k, v)

            self._send_json(200, {"status": "success", "message": "Settings updated successfully."})
            return

        # 4. Add/Update User (/v1/users) - privilege: 'all' required
        if path == "/v1/users":
            if user_privilege != PRIVILEGE_ALL:
                self._send_json(403, {
                    "error": "Forbidden",
                    "message": "Only users with 'all' privilege can manage user accounts.",
                })
                return

            new_user = body_json.get("username", "")
            new_pw = body_json.get("password", "")
            new_priv = body_json.get("privilege", PRIVILEGE_REGULAR)

            if not new_user or not new_pw:
                self._send_json(400, {"error": "Bad Request", "message": "username and password are required."})
                return

            set_user(new_user, new_pw, new_priv)
            self._send_json(200, {
                "status": "success",
                "message": f"User '{new_user}' created/updated with '{new_priv}' privilege.",
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
                    "version": "0.1.0",
                    "ip": device_ip,
                    "port": server_port,
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
