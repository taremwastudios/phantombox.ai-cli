"""Modern chat UI using Textual – OpenCode terminal style."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any, Dict, List, Optional

import httpx
from rich.markdown import Markdown
from textual import events, work
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Container, VerticalScroll, Horizontal
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static, Label, OptionList, TextArea, Input, Button
from textual.widgets.option_list import Option

from phantombox.runtime import list_running, start_model
from phantombox.models import list_downloaded_models, RECOMMENDED_MODELS
from phantombox.config import (
    load_config,
    set_config_val,
    reset_config_val,
    DEFAULT_CONFIG,
)
from phantombox.env import get_workspace_dir
from phantombox.tools import (
    check_tool_permission,
    execute_tool,
    get_tool_definitions,
)
from phantombox.context import (
    get_context_marker_info,
)
from phantombox.providers import (
    fetch_cached_or_default_models,
    fetch_provider_models,
    format_model_display_name,
    get_all_providers,
    get_provider,
    get_provider_key,
    is_provider_connected,
    list_connected_providers,
    parse_model_identifier,
    set_provider_key,
    stream_chat_deltas,
)

# Straight, solid block ASCII logo for phantombox.ai (matches image3.png, filled with white)
ASCII_BANNER = """█▀█ █ █ ▄▀█ █▄ █ ▀█▀ █ █ █▀▄▀█ █▀▄ █ █ ▀▄▀   ▄▀█ █
█▀▀ █▀█ █▀█ █ ▀█  █  █▄█ █ █ █ █▄▀ █▄█ █ █ ▄ █▀█ █
▀   ▀ ▀ ▀ ▀ ▀  ▀  ▀  ▀ ▀ ▀   ▀ ▀▀  ▀ ▀ ▀ ▀   ▀ ▀ ▀"""


class PromptArea(TextArea):
    """Auto-expanding text area that submits on Enter and allows Shift+Enter for newlines."""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        super().__init__(placeholder=placeholder, **kwargs)
        self.show_line_numbers = False

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            # Enter submits, Shift+Enter makes newline
            event.prevent_default()
            event.stop()
            val = self.text.strip()
            self.text = ""
            self.styles.height = 4
            if val:
                self.post_message(self.Submitted(val))
            return
        elif event.key == "shift+enter":
            # Insert newline
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        lines = max(1, min(event.text_area.document.line_count, 10))
        self.styles.height = lines + 3


class SpinnerThinking(Static):
    """Animated braille spinner widget beside Thinking indicator."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, color: str = "#f97316", label_text: str = "Thinking…", **kwargs) -> None:
        self.label_text = label_text
        super().__init__(f"⠋ {label_text}", classes="thinking-text", **kwargs)
        self._frame_idx = 0
        self.spinner_color = color

    def on_mount(self) -> None:
        if self.spinner_color:
            self.styles.color = self.spinner_color
        self.set_interval(0.08, self._next_frame)

    def _next_frame(self) -> None:
        self._frame_idx = (self._frame_idx + 1) % len(self.FRAMES)
        self.update(f"{self.FRAMES[self._frame_idx]} {self.label_text}")


class ModelPickerModal(ModalScreen[str | None]):
    """Modal screen for switching models (shows local models & connected cloud provider models)."""

    DEFAULT_CSS = """
    ModelPickerModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.75);
    }

    #picker-container {
        width: 82;
        max-height: 24;
        background: #161b22;
        border: heavy #f97316;
        padding: 1 2;
    }

    #picker-title {
        color: #f97316;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }

    #picker-options {
        background: #0d1117;
        border: tall #30363d;
        height: auto;
        max-height: 14;
    }

    #picker-empty {
        color: #f87171;
        text-align: center;
        margin: 1 0;
    }

    #picker-help {
        color: #8b949e;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "select_current", "Select"),
    ]

    def __init__(self, current_model: str, options: list[tuple[str, str, bool]], theme_color: str = "#f97316"):
        super().__init__()
        self.current_model = current_model
        self.options = options
        self.theme_color = theme_color
        self._initial_idx = 0

    def compose(self) -> ComposeResult:
        with Container(id="picker-container"):
            yield Static("Select Model (Local & Cloud Providers)", id="picker-title")
            if self.options:
                ol = OptionList(id="picker-options")
                for idx, (model_id, label_text, _) in enumerate(self.options):
                    ol.add_option(Option(label_text, id=model_id))
                    if model_id == self.current_model:
                        self._initial_idx = idx
                    elif self.current_model and self.current_model.lower() in model_id.lower():
                        self._initial_idx = idx
                yield ol
                yield Static("Enter: Select  •  Esc: Cancel  •  ↑/↓: Navigate", id="picker-help")
            else:
                yield Static("No local models or connected cloud providers found.\nRun 'phantombox pull' or '/mcp --connect' to connect providers.", id="picker-empty")
                yield Static("Esc: Close", id="picker-help")

    def on_mount(self) -> None:
        try:
            container = self.query_one("#picker-container")
            container.styles.border = ("heavy", self.theme_color)
            title = self.query_one("#picker-title")
            title.styles.color = self.theme_color
        except Exception:
            pass

        if self.options:
            ol = self.query_one("#picker-options", OptionList)
            ol.highlighted = self._initial_idx
            ol.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select_current(self) -> None:
        try:
            ol = self.query_one("#picker-options", OptionList)
            if ol.highlighted is not None and ol.highlighted < ol.option_count:
                opt = ol.get_option_at_index(ol.highlighted)
                self.dismiss(str(opt.id))
        except Exception:
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))


class SessionPickerModal(ModalScreen[str | None]):
    """Modal screen for selecting, restoring, or deleting saved chat sessions. Press 'd' to delete highlighted session."""

    DEFAULT_CSS = """
    SessionPickerModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }

    #session-picker-container {
        width: 82;
        max-height: 24;
        background: #161b22;
        border: heavy #f97316;
        padding: 1 2;
    }

    #session-picker-title {
        color: #f97316;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }

    #session-picker-options {
        background: #0d1117;
        border: tall #30363d;
        height: auto;
        max-height: 14;
    }

    #session-picker-empty {
        color: #8b949e;
        text-align: center;
        margin: 1 0;
    }

    #session-picker-help {
        color: #8b949e;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "select_current", "Select"),
        ("d", "delete_current", "Delete"),
    ]

    def __init__(self, sessions: list[dict], theme_color: str = "#f97316"):
        super().__init__()
        self.sessions = list(sessions)
        self.theme_color = theme_color

    def compose(self) -> ComposeResult:
        with Container(id="session-picker-container"):
            yield Static("Saved Chat Sessions", id="session-picker-title")
            if self.sessions:
                ol = OptionList(id="session-picker-options")
                for s in self.sessions:
                    from datetime import datetime
                    dt_str = datetime.fromtimestamp(s.get("timestamp", 0)).strftime("%b %d, %H:%M")
                    label = f"● {s.get('name')}  [dim]({s.get('model')}, {s.get('message_count')} msgs, {dt_str})[/dim]"
                    ol.add_option(Option(label, id=s.get("id")))
                yield ol
                yield Static("Enter: Restore  •  d: Delete session  •  Esc: Cancel", id="session-picker-help")
            else:
                yield Static("No saved chat sessions found.\nSave a session using /chat save <name>.", id="session-picker-empty")
                yield Static("Esc: Close", id="session-picker-help")

    def on_mount(self) -> None:
        try:
            container = self.query_one("#session-picker-container")
            container.styles.border = ("heavy", self.theme_color)
            title = self.query_one("#session-picker-title")
            title.styles.color = self.theme_color
        except Exception:
            pass

        if self.sessions:
            ol = self.query_one("#session-picker-options", OptionList)
            ol.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select_current(self) -> None:
        try:
            ol = self.query_one("#session-picker-options", OptionList)
            if ol.highlighted is not None and ol.highlighted < ol.option_count:
                opt = ol.get_option_at_index(ol.highlighted)
                self.dismiss(f"restore:{opt.id}")
        except Exception:
            pass

    def action_delete_current(self) -> None:
        """Handle 'd' key to delete the highlighted session."""
        try:
            ol = self.query_one("#session-picker-options", OptionList)
            if ol.highlighted is not None and ol.highlighted < ol.option_count:
                opt = ol.get_option_at_index(ol.highlighted)
                session_id = str(opt.id)
                from phantombox.sessions import delete_chat_session, list_saved_sessions
                delete_chat_session(session_id)
                self.sessions = list_saved_sessions()
                ol.clear_options()
                for s in self.sessions:
                    from datetime import datetime
                    dt_str = datetime.fromtimestamp(s.get("timestamp", 0)).strftime("%b %d, %H:%M")
                    label = f"● {s.get('name')}  [dim]({s.get('model')}, {s.get('message_count')} msgs, {dt_str})[/dim]"
                    ol.add_option(Option(label, id=s.get("id")))
                if not self.sessions:
                    self.dismiss("deleted_all")
        except Exception:
            pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(f"restore:{event.option.id}")


class ApiKeyInputModal(ModalScreen[bool]):
    """Modal dialog to enter, save, or clear an API key for an external provider."""

    DEFAULT_CSS = """
    ApiKeyInputModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.85);
    }

    #apikey-container {
        width: 76;
        height: auto;
        background: #161b22;
        border: heavy #f97316;
        padding: 1 2;
    }

    #apikey-title {
        color: #f97316;
        text-style: bold;
        text-align: center;
        margin-bottom: 0;
    }

    #apikey-desc {
        color: #8b949e;
        text-align: center;
        margin-bottom: 1;
    }

    #apikey-status {
        text-align: center;
        margin-bottom: 1;
    }

    #apikey-input {
        background: #0d1117;
        border: tall #30363d;
        margin-bottom: 1;
    }

    #apikey-buttons {
        align: center middle;
        height: auto;
        margin-top: 0;
        margin-bottom: 1;
    }

    #apikey-buttons Button {
        margin: 0 1;
    }

    #apikey-help {
        color: #8b949e;
        text-align: center;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, provider_id: str, theme_color: str = "#f97316"):
        super().__init__()
        self.provider_id = provider_id
        self.theme_color = theme_color
        self.p_info = get_provider(provider_id) or {"name": provider_id, "description": ""}

    def compose(self) -> ComposeResult:
        p_name = self.p_info.get("name", self.provider_id)
        desc = self.p_info.get("description", "")
        current_key = get_provider_key(self.provider_id)

        if current_key:
            masked = current_key[:4] + "••••" + current_key[-4:] if len(current_key) > 8 else "••••••••"
            status_text = f"[bold green]Connected[/bold green] (Key: {masked})"
        else:
            status_text = "[dim yellow]Not connected[/dim yellow]"

        with Container(id="apikey-container"):
            yield Static(f"Connect Provider: {p_name}", id="apikey-title")
            if desc:
                yield Static(desc, id="apikey-desc")
            yield Static(status_text, id="apikey-status")
            yield Input(
                placeholder=f"Enter {p_name} API Key...",
                id="apikey-input",
                password=True,
            )
            with Horizontal(id="apikey-buttons"):
                yield Button("Save Key", id="btn-save", variant="primary")
                if current_key:
                    yield Button("Disconnect", id="btn-disconnect", variant="error")
                yield Button("Cancel", id="btn-cancel")
            yield Static("Enter: Save  •  Esc: Cancel", id="apikey-help")

    def on_mount(self) -> None:
        try:
            container = self.query_one("#apikey-container")
            container.styles.border = ("heavy", self.theme_color)
            title = self.query_one("#apikey-title")
            title.styles.color = self.theme_color
        except Exception:
            pass
        self.query_one("#apikey-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._save_and_dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-save":
            self._save_and_dismiss()
        elif btn_id == "btn-disconnect":
            set_provider_key(self.provider_id, "")
            self.dismiss(True)
        elif btn_id == "btn-cancel":
            self.dismiss(False)

    def _save_and_dismiss(self) -> None:
        inp = self.query_one("#apikey-input", Input)
        val = inp.value.strip()
        if val:
            set_provider_key(self.provider_id, val)
            self.dismiss(True)
        else:
            self.dismiss(False)


class McpConnectModal(ModalScreen[None]):
    """Modal screen for connecting external MCP / Cloud LLM providers (OpenRouter, Groq, Claude, OpenAI, etc.)."""

    DEFAULT_CSS = """
    McpConnectModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }

    #mcp-container {
        width: 86;
        max-height: 28;
        background: #161b22;
        border: heavy #f97316;
        padding: 1 2;
    }

    #mcp-title {
        color: #f97316;
        text-style: bold;
        text-align: center;
        margin-bottom: 0;
    }

    #mcp-info {
        color: #8b949e;
        text-align: center;
        margin-bottom: 1;
    }

    #mcp-options {
        background: #0d1117;
        border: tall #30363d;
        height: auto;
        max-height: 14;
    }

    #mcp-help {
        color: #8b949e;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "select_current", "Select"),
    ]

    def __init__(self, theme_color: str = "#f97316"):
        super().__init__()
        self.theme_color = theme_color

    def compose(self) -> ComposeResult:
        with Container(id="mcp-container"):
            yield Static("MCP & External Model Providers", id="mcp-title")
            yield Static("Connect API keys to use cloud models in Phantombox chat & /models", id="mcp-info")
            yield OptionList(id="mcp-options")
            yield Static("Enter: Connect / Edit Key  •  Esc: Close  •  ↑/↓: Navigate", id="mcp-help")

    def on_mount(self) -> None:
        try:
            container = self.query_one("#mcp-container")
            container.styles.border = ("heavy", self.theme_color)
            title = self.query_one("#mcp-title")
            title.styles.color = self.theme_color
        except Exception:
            pass
        self._refresh_options()

    def _refresh_options(self) -> None:
        ol = self.query_one("#mcp-options", OptionList)
        highlighted_id = None
        if ol.highlighted is not None and ol.highlighted < ol.option_count:
            try:
                highlighted_id = ol.get_option_at_index(ol.highlighted).id
            except Exception:
                pass

        ol.clear_options()
        all_providers = get_all_providers()

        for p in all_providers:
            pid = p["id"]
            connected = is_provider_connected(pid)
            dot = "●" if connected else "○"
            status_tag = "[CONNECTED]    " if connected else "[NOT CONNECTED]"
            label = f"{dot} {status_tag} {p['name']:<18} ({p['description']})"
            ol.add_option(Option(label, id=f"provider_{pid}"))

        if highlighted_id:
            for idx in range(ol.option_count):
                if ol.get_option_at_index(idx).id == highlighted_id:
                    ol.highlighted = idx
                    break
        ol.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select_current(self) -> None:
        ol = self.query_one("#mcp-options", OptionList)
        if ol.highlighted is not None and ol.highlighted < ol.option_count:
            opt = ol.get_option_at_index(ol.highlighted)
            self._handle_provider_select(str(opt.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        opt_id = str(event.option.id or event.option_id)
        self._handle_provider_select(opt_id)

    def _handle_provider_select(self, opt_id: str) -> None:
        if opt_id.startswith("provider_"):
            provider_id = opt_id.replace("provider_", "")
            p_spec = get_provider(provider_id)
            if p_spec and p_spec.get("no_key_required"):
                # No API key required for phantombox router
                self.app.notify("PhantomBox Smart Router is enabled by default. Select 'phantombox/router' in /models.", title="PhantomBox Router")
                return

            def on_key_modal_closed(changed: bool) -> None:
                self._refresh_options()
                if is_provider_connected(provider_id):
                    asyncio.create_task(fetch_provider_models(provider_id))

            self.app.push_screen(ApiKeyInputModal(provider_id, theme_color=self.theme_color), on_key_modal_closed)


class SettingsModal(ModalScreen[None]):
    """Modal screen for configuring permission levels and path scopes."""

    DEFAULT_CSS = """
    SettingsModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }

    #settings-container {
        width: 86;
        max-height: 32;
        background: #161b22;
        border: heavy #f97316;
        padding: 1 2;
    }

    #settings-title {
        color: #f97316;
        text-style: bold;
        text-align: center;
        margin-bottom: 0;
    }

    #settings-ws-info {
        color: #8b949e;
        text-align: center;
        margin-bottom: 1;
    }

    #settings-options {
        background: #0d1117;
        border: tall #30363d;
        height: auto;
        max-height: 18;
    }

    #settings-help {
        color: #8b949e;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "select_current", "Select"),
    ]

    def __init__(self, theme_color: str = "#f97316"):
        super().__init__()
        self.theme_color = theme_color

    def compose(self) -> ComposeResult:
        ws_path = str(get_workspace_dir())
        with Container(id="settings-container"):
            yield Static("Settings: Tools & Permissions", id="settings-title")
            yield Static(f"AI Workspace: {ws_path}", id="settings-ws-info")
            yield OptionList(id="settings-options")
            yield Static("Enter: Toggle / Select  •  Esc: Save & Close", id="settings-help")

    def on_mount(self) -> None:
        try:
            container = self.query_one("#settings-container")
            container.styles.border = ("heavy", self.theme_color)
            title = self.query_one("#settings-title")
            title.styles.color = self.theme_color
        except Exception:
            pass
        self._refresh_options()

    def _refresh_options(self) -> None:
        cfg = load_config()
        mode = cfg.get("permission_mode", "restricted")
        reads = cfg.get("allowed_read_paths", ["*"])
        writes = cfg.get("allowed_write_paths", ["*"])
        custom_auto = cfg.get("custom_auto_approve_tools", ["list_dir", "read_file", "create_file", "create_folder", "internet_search"])
        online_search = cfg.get("enable_internet_search", True)

        ol = self.query_one("#settings-options", OptionList)
        highlighted_id = None
        if ol.highlighted is not None and ol.highlighted < ol.option_count:
            try:
                highlighted_id = ol.get_option_at_index(ol.highlighted).id
            except Exception:
                pass

        ol.clear_options()

        # Modes
        r_tag = "[ACTIVE] " if mode == "restricted" else "         "
        c_tag = "[ACTIVE] " if mode == "custom" else "         "
        f_tag = "[ACTIVE] " if mode == "full_access" else "         "

        ol.add_option(Option(f"{r_tag}Mode: Restricted (Every tool run requires confirmation)", id="mode_restricted"))
        ol.add_option(Option(f"{c_tag}Mode: Custom Config (Configure per-tool auto-approval)", id="mode_custom"))
        ol.add_option(Option(f"{f_tag}Mode: Full Access (All tools run without confirmation)", id="mode_full_access"))

        # Scopes within phantombox-dir
        read_scope_str = "All files (*)" if "*" in reads else ", ".join(reads)
        write_scope_str = "All files (*)" if "*" in writes else ", ".join(writes)
        ol.add_option(Option(f"  • Read Scope (in phantombox-dir): {read_scope_str} (Enter to toggle)", id="toggle_read"))
        ol.add_option(Option(f"  • Write Scope (in phantombox-dir): {write_scope_str} (Enter to toggle)", id="toggle_write"))

        # Internet search toggle
        search_status = "[ENABLED]" if online_search else "[DISABLED]"
        ol.add_option(Option(f"  • Online Internet Search: {search_status} (Enter to toggle)", id="toggle_search"))

        # Thinking / Deep reasoning toggle
        thinking_enabled = cfg.get("enable_thinking", False)
        thinking_status = "[ENABLED]" if thinking_enabled else "[DISABLED]"
        ol.add_option(Option(f"  • Thinking / Deep Reasoning: {thinking_status} (Enter to toggle)", id="toggle_thinking"))

        # Custom tool permissions
        if mode == "custom":
            all_tools = ["list_dir", "read_file", "create_file", "create_folder", "write_file", "edit_file", "move_file", "delete_file", "internet_search", "run_command", "call_subagent"]
            for tool_name in all_tools:
                is_auto = tool_name in custom_auto
                status = "[AUTO-RUN]" if is_auto else "[ASK CONFIRMATION]"
                ol.add_option(Option(f"    - Tool '{tool_name}': {status}", id=f"tool_{tool_name}"))

        # Restore highlight
        if highlighted_id:
            for idx in range(ol.option_count):
                if ol.get_option_at_index(idx).id == highlighted_id:
                    ol.highlighted = idx
                    break
        ol.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select_current(self) -> None:
        ol = self.query_one("#settings-options", OptionList)
        if ol.highlighted is not None and ol.highlighted < ol.option_count:
            opt = ol.get_option_at_index(ol.highlighted)
            self._apply_option_toggle(str(opt.id))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        opt_id = str(event.option.id or event.option_id)
        self._apply_option_toggle(opt_id)

    def _apply_option_toggle(self, opt_id: str) -> None:
        cfg = load_config()

        if opt_id == "mode_restricted":
            set_config_val("permission_mode", "restricted")
        elif opt_id == "mode_custom":
            set_config_val("permission_mode", "custom")
        elif opt_id == "mode_full_access":
            set_config_val("permission_mode", "full_access")
        elif opt_id == "toggle_read":
            reads = cfg.get("allowed_read_paths", ["*"])
            set_config_val("allowed_read_paths", ["."] if "*" in reads else ["*"])
        elif opt_id == "toggle_write":
            writes = cfg.get("allowed_write_paths", ["*"])
            set_config_val("allowed_write_paths", ["."] if "*" in writes else ["*"])
        elif opt_id == "toggle_search":
            cur_search = cfg.get("enable_internet_search", True)
            set_config_val("enable_internet_search", not cur_search)
        elif opt_id == "toggle_thinking":
            cur_thinking = cfg.get("enable_thinking", False)
            set_config_val("enable_thinking", not cur_thinking)
        elif opt_id.startswith("tool_"):
            tool_name = opt_id.replace("tool_", "")
            custom_auto = list(cfg.get("custom_auto_approve_tools", []))
            if tool_name in custom_auto:
                custom_auto.remove(tool_name)
            else:
                custom_auto.append(tool_name)
            set_config_val("custom_auto_approve_tools", custom_auto)

        self._refresh_options()


def format_assistant_markdown(text: str) -> str:
    """Format assistant reply, styling <think>...</think> reasoning blocks cleanly."""
    if "<think>" not in text:
        return text

    if "</think>" in text:
        parts = text.split("</think>", 1)
        think_part = parts[0].replace("<think>", "").strip()
        answer_part = parts[1].strip()
        if think_part:
            lines = [f"> *{line}*" if line.strip() else ">" for line in think_part.splitlines()]
            think_formatted = "**Thought Process:**\n" + "\n".join(lines)
            if answer_part:
                return f"{think_formatted}\n\n---\n\n{answer_part}"
            return think_formatted
        return answer_part
    else:
        # Currently in active thinking stream
        think_part = text.replace("<think>", "").strip()
        lines = [f"> *{line}*" if line.strip() else ">" for line in think_part.splitlines()]
        return "**Thinking...**\n" + "\n".join(lines)


class ChatMessage(Container):
    """A chat message widget supporting user, assistant, system, and tool roles."""

    def __init__(
        self,
        sender_name: str,
        content: str,
        role: str,
        sender_color: str = "",
    ) -> None:
        super().__init__(classes=f"message-row role-{role}")
        self.sender_name = sender_name
        self.content = content
        self.role = role
        self.sender_color = sender_color

    def compose(self) -> ComposeResult:
        header = Static(f"● {self.sender_name}", classes="message-header")
        if self.sender_color:
            header.styles.color = self.sender_color
        yield header
        if self.role == "assistant":
            yield Static(Markdown(format_assistant_markdown(self.content)), classes="message-body")
            yield Static("", classes="message-imprint", id="message-imprint-line")
        else:
            yield Static(self.content, classes="message-body")

    def update_content(self, new_content: str) -> None:
        self.content = new_content
        body = self.query_one(".message-body", Static)
        if self.role == "assistant":
            # Check if router imprint is present at end of message
            # e.g.: --- \n *Response returned by: **model** via **phantombox** after 1.2s*
            imprint_marker = "\n\n---\n*Response returned by: "
            if imprint_marker in new_content:
                main_text, imprint_text = new_content.split(imprint_marker, 1)
                body.update(Markdown(format_assistant_markdown(main_text)))

                try:
                    imprint_line = self.query_one("#message-imprint-line", Static)
                    # Parse model, provider/subagent tag, and latency
                    # Example imprint_text: "**groq/llama-3.3-70b** via **phantombox** (subagent1) after 1.2s*"
                    clean_imp = imprint_text.rstrip("*").strip()
                    parts = clean_imp.split(" via ")
                    model_part = parts[0].replace("**", "").strip() if parts else ""
                    rest = parts[1] if len(parts) > 1 else ""

                    via_part = "phantombox"
                    sub_part = ""
                    time_part = ""
                    if " after " in rest:
                        after_split = rest.split(" after ")
                        time_part = after_split[1].replace("*", "").strip()
                        mid = after_split[0]
                    else:
                        mid = rest

                    if "(subagent1)" in mid:
                        sub_part = " [bold cyan](subagent1)[/bold cyan]"

                    colored_footer = (
                        f"[dim]───[/dim]\n"
                        f"[dim]Response returned by:[/dim] [bold cyan]{model_part}[/bold cyan] "
                        f"[dim]via[/dim] [bold #f97316]{via_part}[/bold #f97316]{sub_part} "
                        f"[dim]after[/dim] [bold green]{time_part}[/bold green]"
                    )
                    imprint_line.update(colored_footer)
                    imprint_line.styles.display = "block"
                except Exception:
                    pass
            else:
                body.update(Markdown(format_assistant_markdown(new_content)))
        else:
            body.update(new_content)


class SuggestionsRow(Container):
    """Interactive selectable suggestions rendered from subagent."""

    class Selected(Message):
        def __init__(self, suggestion_text: str) -> None:
            super().__init__()
            self.suggestion_text = suggestion_text

    def __init__(self, agent_name: str, suggestions: list[str]) -> None:
        super().__init__(classes="suggestions-row")
        self.agent_name = agent_name
        self.suggestions = suggestions

    def compose(self) -> ComposeResult:
        yield Static(f"💡 Subagent ({self.agent_name}) Suggestions (Click or Enter to send):", classes="suggestions-header")
        ol = OptionList(classes="suggestions-options")
        for idx, s in enumerate(self.suggestions):
            clean_s = s.strip().lstrip("•-*0123456789. \t").strip()
            low_s = clean_s.lower()
            if not clean_s or len(clean_s) < 2:
                continue
            # Double check against any provider / router imprint text
            if "response returned by" in low_s or "returned by:" in low_s or "via phantombox" in low_s:
                continue
            if "notice: all cloud" in low_s or "router alert" in low_s:
                continue
            ol.add_option(Option(f"  ➜ {clean_s}", id=clean_s))
        yield ol

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        chosen_text = str(event.option.id or event.option_id)
        self.post_message(self.Selected(chosen_text))


class PhantomChat(App):
    """Main chat application styled after OpenCode."""

    CSS = """
    Screen {
        background: #0d1117;
        color: #e6edf3;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }

    * {
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }

    VerticalScroll {
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }

    /* =========================================================================
       Welcome Screen (Hero ASCII Banner + Centered Prompt Box)
       ========================================================================= */
    #welcome-container {
        width: 100%;
        height: 1fr;
        align: center middle;
    }

    #welcome-wrapper {
        width: 90;
        height: auto;
        align: center middle;
    }

    #hero-banner {
        width: 100%;
        content-align: center middle;
        text-align: center;
        color: #ffffff;
        text-style: bold;
        margin-bottom: 2;
    }

    /* OpenCode prompt card with bottom bar - increased height */
    #welcome-prompt-card {
        width: 100%;
        height: auto;
        background: #161b22;
        border: round #f97316;
        padding: 0;
        margin-bottom: 1;
    }

    #welcome-prompt-card:focus-within {
        border: round #f97316;
    }

    #welcome-prompt-card PromptArea {
        width: 100%;
        background: transparent;
        border: none;
        color: #e6edf3;
        padding: 1 1 0 1;
        height: 4;
    }

    #welcome-box-footer {
        width: 100%;
        height: 1;
        background: #0d1117;
        padding: 0 2;
    }

    .box-model-tag {
        color: #f97316;
        text-style: bold;
    }

    .box-context-tag {
        text-style: bold;
        margin-left: 1;
    }

    .box-footer-spacer {
        width: 1fr;
    }

    .box-hint-tag {
        color: #6e7681;
    }

    #welcome-commands {
        width: 100%;
        text-align: center;
        color: #6e7681;
        margin-top: 1;
    }

    /* =========================================================================
       Active Chat Log View
       ========================================================================= */
    #chat-view {
        display: none;
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
        scrollbar-size-vertical: 0;
    }

    /* Docked prompt box at bottom during chat - increased height */
    #dock-container {
        dock: bottom;
        display: none;
        background: #0d1117;
        padding: 1 2 1 2;
        height: auto;
    }

    #dock-prompt-card {
        width: 100%;
        height: auto;
        background: #161b22;
        border: round #f97316;
        padding: 0;
    }

    #dock-prompt-card:focus-within {
        border: round #f97316;
    }

    #dock-prompt-card PromptArea {
        width: 100%;
        background: transparent;
        border: none;
        color: #e6edf3;
        padding: 1 1 0 1;
        height: 4;
    }

    #dock-box-footer {
        width: 100%;
        height: 1;
        background: #090d13;
        padding: 0 2;
    }

    /* Message rows */
    .message-row {
        height: auto;
        margin: 1 0;
        padding: 0 1;
    }

    .message-header {
        text-style: bold;
        margin-bottom: 0;
    }

    .role-user .message-header {
        color: #58a6ff;
    }

    .role-assistant .message-header {
        color: #f97316;
    }

    .role-system .message-header {
        color: #8b949e;
    }

    .role-tool .message-header {
        color: #eab308;
    }

    .role-search .message-header {
        color: #38bdf8;
    }

    .role-user .message-body {
        background: #21262d;
        color: #e6edf3;
        padding: 1 2;
        margin: 0 4 0 0;
        border-left: thick #58a6ff;
    }

    .role-assistant .message-body {
        color: #e6edf3;
        padding: 0 1;
        margin: 0;
    }

    .message-imprint {
        padding: 0 1;
        margin-top: 1;
        height: auto;
        display: none;
    }

    .role-system .message-body {
        color: #8b949e;
        text-style: italic;
        padding: 0 1;
    }

    .role-tool .message-body {
        background: #1c1917;
        color: #fde047;
        border-left: thick #eab308;
        padding: 1 2;
        margin: 0 2;
    }

    .role-search .message-body {
        background: #082f49;
        color: #bae6fd;
        border-left: thick #38bdf8;
        padding: 1 2;
        margin: 0 2;
    }

    /* Thinking spinner block */
    #thinking-row {
        height: auto;
        margin: 1 0;
        padding: 0 1;
    }

    #active-stream-spinner {
        height: auto;
        margin: 0 0 1 1;
        padding: 0;
    }

    .thinking-text {
        color: #f97316;
        text-style: bold;
        padding: 0 1;
    }

    /* Subagent Suggestions interactive row */
    .suggestions-row {
        height: auto;
        margin: 1 0;
        padding: 0 1;
    }

    .suggestions-header {
        color: #38bdf8;
        text-style: bold;
        margin-bottom: 0;
    }

    .suggestions-options {
        background: #0d1117;
        border: tall #38bdf8;
        height: auto;
        max-height: 8;
        margin-top: 0;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+q", "quit", "Quit"),
        ("escape", "stop_generation", "Stop"),
    ]

    def action_stop_generation(self) -> None:
        if self._is_generating:
            self._stop_requested = True
            if self._current_thinking_container:
                try:
                    self._current_thinking_container.remove()
                except Exception:
                    pass
                self._current_thinking_container = None
            self._is_generating = False
            self.run_worker(self._add_system_message("[bold yellow]Generation stopped by user.[/bold yellow]"))

    def _build_system_prompt(self) -> str:
        from datetime import datetime

        ws_dir = str(get_workspace_dir())
        cfg = load_config()
        search_enabled = cfg.get("enable_internet_search", True)
        thinking_enabled = cfg.get("enable_thinking", False)
        current_date_str = datetime.now().strftime("%A, %B %d, %Y")

        # Subagent list injection so models know exactly which subagents are available
        from phantombox.subagents import list_subagents
        active_subagents = list_subagents()
        if active_subagents:
            subagent_items = []
            for nick, info in active_subagents.items():
                role = info.get("role", "Helper")
                scripts = ", ".join(info.get("scripts", [])) if info.get("scripts") else "general"
                subagent_items.append(f"     * '{nick}' (Role: {role}, Capabilities: {scripts})")
            subagents_rule = (
                "9. SUBAGENTS DELEGATION DIRECTIVE:\n"
                "   The user has configured the following active subagents:\n"
                + "\n".join(subagent_items) + "\n"
                "   CRITICAL SUBAGENT INSTRUCTIONS:\n"
                "   - You MUST call subagents ONLY using one of the exact nicknames listed above.\n"
                "   - NEVER invent or hallucinate new subagent nicknames that are not in the list.\n"
                "   - When delegating background coding, code audits, terminal jobs, file operations, or research, call `call_subagent(nickname, task)`.\n"
            )
        else:
            subagents_rule = (
                "9. SUBAGENTS DIRECTIVE:\n"
                "   There are currently NO subagents registered on this machine.\n"
                "   Do NOT attempt to invent or call any subagents until the user sets them up using `/subagents setup <nickname> <model>`.\n"
            )

        search_rule = (
            f"6. REAL-TIME SEARCH DIRECTIVE (Current Date: {current_date_str}):\n"
            "   You possess proactive internet search capability (like Grok / Perplexity).\n"
            "   You do NOT need the user to explicitly tell you to 'search online'.\n"
            "   Whenever a user asks about:\n"
            "   - Current events, breaking news, or recent developments\n"
            "   - Specific dates, years, timelines, or recent updates\n"
            "   - Sports scores, matches, tournaments, or standings\n"
            "   - People, leadership, politics, elections, or appointments\n"
            "   - Software releases, latest versions, or tech announcements\n"
            "   - Weather, prices, market information, or live data\n"
            "   - Any topic where your pre-trained knowledge could be outdated, incomplete, or uncertain\n"
            "   You MUST proactively invoke `internet_search` with targeted keywords BEFORE formulating your response.\n"
            "   Never guess, extrapolate, or hallucinate dates, facts, or outcomes when live search is available.\n"
            if search_enabled
            else "6. Internet search is currently DISABLED by user settings. Rely on your internal knowledge without calling internet_search.\n"
        )

        thinking_rule = (
            "7. DEEP REASONING & THINKING DIRECTIVE:\n"
            "   Before answering, you MUST think, reason, and clarify steps with yourself within <think>...</think> tags.\n"
            "   Break down the problem, verify facts and dates, plan tool executions, and evaluate your logic.\n"
            "   After the </think> tag, output your clean, direct final response to the user.\n"
            if thinking_enabled
            else ""
        )

        return (
            "You are a helpful assistant with execution tools (list_dir, read_file, create_file, create_folder, write_file, edit_file, move_file, delete_file, internet_search, run_command, call_subagent). "
            f"Your working directory is designated as 'phantombox-dir' at: {ws_dir}.\n\n"
            "CRITICAL TOOL INSTRUCTIONS (You MUST strictly follow these rules):\n"
            "1. ALWAYS invoke tools using standard function calls — NEVER simulate, fake, or invent tool outputs in plain chat text.\n"
            "2. When writing, saving, or generating code/text to disk, ALWAYS execute create_file or write_file with the full content. "
            "NEVER merely show code blocks and claim you saved it.\n"
            "3. When reading or exploring files and folders, ALWAYS use list_dir and read_file first before making assumptions.\n"
            "4. For modifying existing files, prefer edit_file with the exact snippet to replace, or write_file to overwrite. "
            "Use move_file to move or rename files, and delete_file to remove files/directories.\n"
            "5. All file paths in arguments must be relative to 'phantombox-dir' (e.g., 'main.py' or 'src/utils.py').\n"
            f"{search_rule}"
            f"{thinking_rule}"
            "8. When asked to run terminal/shell processes, commands, builds, or scripts, use `run_command` with the exact shell command to execute in phantombox-dir.\n"
            f"{subagents_rule}"
            "10. Never fabricate factual information: proactively verify using search or local tools first whenever possible."
        )

    def _sync_system_prompt(self) -> None:
        """Update system prompt in active conversation to reflect current settings."""
        new_prompt = self._build_system_prompt()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = new_prompt
        else:
            self.messages.insert(0, {"role": "system", "content": new_prompt})

    def __init__(self, model_name: str, port: int):
        super().__init__()
        self.model_name = model_name
        self.port = port

        # Load persistent configuration
        cfg = load_config()
        self.user_name = cfg.get("user_name", "User")
        self.assistant_name = cfg.get("assistant_name", "Assistant")
        self.theme_color = cfg.get("theme_color", "#f97316")
        self.user_color = cfg.get("user_color", "#58a6ff")
        self.assistant_color = cfg.get("assistant_color", "#f97316")

        # Subagents active in this session
        self.session_subagent: Optional[str] = None
        # Auto-assign if global_auto is set
        sub_cfg = cfg.get("subagents", {})
        if sub_cfg.get("global_auto") and sub_cfg.get("global_auto_nickname"):
            self.session_subagent = sub_cfg.get("global_auto_nickname")

        self.messages: List[dict] = [
            {"role": "system", "content": self._build_system_prompt()}
        ]
        self.in_welcome_mode = True

        # Tool confirmation future for pause/resume
        self._pending_confirmation: Optional[asyncio.Future[str]] = None

        # Generation control & stop handling
        self._is_generating: bool = False
        self._stop_requested: bool = False
        self._current_thinking_container: Optional[Container] = None

    def compose(self) -> ComposeResult:
        with Container(id="welcome-container"):
            with Container(id="welcome-wrapper"):
                yield Static(ASCII_BANNER, id="hero-banner")
                with Container(id="welcome-prompt-card"):
                    yield PromptArea(
                        placeholder="Ask phantombox anything, or type / to run a command...",
                        id="welcome-prompt",
                    )
                    with Horizontal(id="welcome-box-footer"):
                        yield Label(f"● {format_model_display_name(self.model_name)}", id="welcome-model-tag", classes="box-model-tag")
                        yield Label("", id="welcome-context-tag", classes="box-context-tag")
                        yield Static(classes="box-footer-spacer")
                        yield Label("Run /help for commands", classes="box-hint-tag")
                yield Static(
                    "[dim]/settings[/dim] Permissions  •  [dim]/models[/dim] Switch model  •  [dim]/server[/dim] LAN Server  •  [dim]/mcp --connect[/dim] Connect APIs  •  [dim]/color[/dim] Theme",
                    id="welcome-commands",
                )

        yield VerticalScroll(id="chat-view")

        with Container(id="dock-container"):
            with Container(id="dock-prompt-card"):
                yield PromptArea(
                    placeholder="Ask phantombox anything, or type / to run a command...",
                    id="dock-prompt",
                )
                with Horizontal(id="dock-box-footer"):
                    yield Label(f"● {format_model_display_name(self.model_name)}", id="dock-model-tag", classes="box-model-tag")
                    yield Label("", id="dock-context-tag", classes="box-context-tag")
                    yield Static(classes="box-footer-spacer")
                    yield Label("Run /help for commands", classes="box-hint-tag")

    def on_mount(self) -> None:
        self._apply_border_color(self.theme_color)
        self._update_status_model()
        self.query_one("#welcome-prompt", PromptArea).focus()
        # Hot-load cloud provider model catalogs right when the chat opens so
        # /models shows the full list immediately. Previously the catalogs were
        # only fetched after re-saving an API key in /mcp --connect, so a fresh
        # chat session showed a tiny models list until keys were re-entered.
        self._warm_provider_model_cache()

    def _warm_provider_model_cache(self) -> None:
        """Pre-fetch model catalogs from every connected provider at chat startup.

        Fills the in-memory model cache so /models lists full catalogs immediately,
        without requiring the user to re-save API keys via /mcp --connect.
        """
        try:
            for pid in list_connected_providers():
                if pid == "phantombox":
                    continue
                asyncio.create_task(fetch_provider_models(pid))
        except Exception:
            # Never block chat startup over catalog warm-up failures
            pass

    def _apply_border_color(self, hex_or_name: str) -> None:
        """Dynamically set the border color on both prompt cards and model tags."""
        for card_id in ("#welcome-prompt-card", "#dock-prompt-card"):
            try:
                card = self.query_one(card_id)
                card.styles.border = ("round", hex_or_name)
            except Exception:
                pass

        for tag_id in ("#welcome-model-tag", "#dock-model-tag"):
            try:
                tag = self.query_one(tag_id, Label)
                tag.styles.color = hex_or_name
            except Exception:
                pass

    def _switch_to_chat(self) -> None:
        """Switch view from centered welcome banner to the active chat log."""
        if self.in_welcome_mode:
            self.query_one("#welcome-container").styles.display = "none"
            self.query_one("#chat-view").styles.display = "block"
            self.query_one("#dock-container").styles.display = "block"
            self.in_welcome_mode = False
            self.query_one("#dock-prompt", PromptArea).focus()

    def _update_status_model(self) -> None:
        display_name = format_model_display_name(self.model_name)
        ctx_info = get_context_marker_info(self.messages, self.model_name)

        for selector in ("#welcome-model-tag", "#dock-model-tag"):
            try:
                lbl = self.query_one(selector, Label)
                lbl.update(f"● {display_name}")
            except Exception:
                pass

        for selector in ("#welcome-context-tag", "#dock-context-tag"):
            try:
                lbl = self.query_one(selector, Label)
                if self.model_name == "(no model selected)":
                    lbl.update("")
                else:
                    lbl.update(ctx_info["text"])
                    lbl.styles.color = ctx_info["color"]
            except Exception:
                pass

    async def _add_system_message(self, text: str) -> None:
        chat_view = self.query_one("#chat-view")
        msg = ChatMessage("System", text, "system")
        await chat_view.mount(msg)
        chat_view.scroll_end(animate=False)

    async def _add_tool_message(self, title: str, text: str) -> None:
        chat_view = self.query_one("#chat-view")
        msg = ChatMessage(f"Tool Result ({title})", text, "tool")
        await chat_view.mount(msg)
        chat_view.scroll_end(animate=False)

    async def _add_search_indicator(self, pages_count: int, query: str = "") -> None:
        chat_view = self.query_one("#chat-view")
        if pages_count <= 0:
            label = "Searched the web"
        elif pages_count == 1:
            label = "Searched 1 page"
        else:
            label = f"Searched {pages_count} pages"

        if query:
            content = f"🔍 {label} for '[italic]{query}[/italic]'"
        else:
            content = f"🔍 {label}"
        msg = ChatMessage("Internet Search", content, "search")
        await chat_view.mount(msg)
        chat_view.scroll_end(animate=False)

    async def on_suggestions_row_selected(self, event: SuggestionsRow.Selected) -> None:
        """When user clicks or presses Enter on a suggestion, immediately submit it as user message."""
        chosen_prompt = event.suggestion_text.strip()
        if chosen_prompt:
            self._switch_to_chat()
            await self._send_user_message(chosen_prompt)

    async def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        raw_text = event.value.strip()
        if not raw_text:
            return

        # Check for /stop immediately
        if raw_text.lower() in ("/stop", "/cancel", "/abort"):
            if self._is_generating:
                self._stop_requested = True
                if self._current_thinking_container:
                    try:
                        await self._current_thinking_container.remove()
                    except Exception:
                        pass
                    self._current_thinking_container = None
                self._is_generating = False
                await self._add_system_message("[bold yellow]Generation stopped by user.[/bold yellow]")
            else:
                await self._add_system_message("No model generation is currently running.")
            return

        # Check if waiting for tool confirmation
        if self._pending_confirmation and not self._pending_confirmation.done():
            self._pending_confirmation.set_result(raw_text)
            self._pending_confirmation = None
            return

        # Handle slash commands
        if raw_text.startswith("/"):
            await self._handle_command(raw_text)
            return

        # Regular user message -> ensure chat view is active
        self._switch_to_chat()
        await self._send_user_message(raw_text)

    async def _handle_command(self, cmd_line: str) -> None:
        parts = cmd_line.split()
        if not parts:
            return
        cmd = parts[0].lower()
        args = parts[1:]
        raw_arg = cmd_line[len(parts[0]):].strip()

        if cmd in ("/exit", "/quit", "/q"):
            self.exit()
            return

        if cmd in ("/stop", "/cancel", "/abort"):
            if self._is_generating:
                self._stop_requested = True
                if self._current_thinking_container:
                    try:
                        await self._current_thinking_container.remove()
                    except Exception:
                        pass
                    self._current_thinking_container = None
                self._is_generating = False
                await self._add_system_message("[bold yellow]Generation stopped by user.[/bold yellow]")
            else:
                await self._add_system_message("No model generation is currently running.")
            return

        if cmd in ("/thinking", "/reasoning"):
            await self._handle_thinking_toggle(raw_arg)
            return

        if cmd in ("/mcp", "/provider", "/providers"):
            await self._handle_mcp_command(args, raw_arg)
            return

        if cmd in ("/settings", "/config"):
            await self._handle_settings_command(args)
            return

        if cmd in ("/search", "/web", "/internet"):
            await self._handle_settings_command(["search"] + args)
            return

        if cmd == "/user":
            await self._handle_user_command(args, raw_arg)
            return

        if cmd in ("/assistant", "/asssiatnt", "/asst"):
            await self._handle_assistant_command(args, raw_arg)
            return

        if cmd in ("/color", "/theme"):
            await self._handle_color_command(args, raw_arg)
            return

        if cmd == "/server":
            await self._handle_server_command(args, raw_arg)
            return

        if cmd in ("/subagents", "/subagent", "/subangents", "/subangent"):
            await self._handle_subagents_command(args, raw_arg)
            return

        if cmd == "/chat":
            await self._handle_chat_command(args, raw_arg)
            return

        if cmd == "/phantombox":
            await self._handle_phantombox_command(args, raw_arg)
            return

        if cmd == "/models":
            if raw_arg:
                if "--thinking" in raw_arg.lower():
                    t_val = ""
                    for i, a in enumerate(args):
                        if a.lower() == "--thinking" and i + 1 < len(args):
                            t_val = args[i + 1]
                            break
                        elif a.lower().startswith("--thinking="):
                            t_val = a.split("=", 1)[1]
                            break
                    await self._handle_thinking_toggle(t_val)
                else:
                    await self._switch_to_model(raw_arg)
            else:
                self._open_models_picker()
            return

        if cmd in ("/clear", "/new"):
            self.messages = [
                {
                    "role": "system",
                    "content": self._build_system_prompt(),
                }
            ]
            self._update_status_model()
            chat_view = self.query_one("#chat-view")
            await chat_view.remove_children()
            self._switch_to_chat()
            await self._add_system_message("Session and context cleared.")
            return

        if cmd == "/help":
            self._switch_to_chat()
            help_text = (
                "Available commands:\n"
                "  • /stop - Stop the model from answering (or press Esc)\n"
                "  • /models --thinking <yes|no> - Toggle deep reasoning / thinking mode (or /thinking yes|no)\n"
                "  • /phantombox --routes - Show (or rescan) every cloud model returning 200 OK\n"
                "  • /models - Switch between local models & connected cloud models\n"
                "  • /settings - Open interactive permissions & tool scope settings\n"
                "  • /settings search <on|off> - Toggle live internet search (or /search on|off)\n"
                "  • /settings mode <restricted|custom|full_access> - Set permission mode\n"
                "  • /settings read <path|*> - Set allowed folder/file scope inside phantombox-dir\n"
                "  • /settings write <path|*> - Set allowed folder/file scope inside phantombox-dir\n"
                "  • /user <name> - Set user name for this session\n"
                "  • /user --color <color> - Set user display name color (e.g. /user --color cyan)\n"
                "  • /user --global <name> - Save user name permanently across sessions\n"
                "  • /assistant <name> - Set assistant name for this session\n"
                "  • /assistant --color <color> - Set assistant display name color\n"
                "  • /color <color> - Change prompt border color\n"
                "  • /server - View server status, configured users & discovery settings\n"
                "  • /server start [port] - Start LAN AI server on this device\n"
                "  • /server close (or /server stop) - Close and stop LAN server\n"
                "  • /server user <name> <password> <all|regular|use-only> - Create user with privilege\n"
                "  • /server users - List users and assigned privilege levels\n"
                "  • /server discoverable <on|off> - Set broadcast discovery for phones/laptops\n"
                "  • /server --discover - Scan LAN/hotspot for other PhantomBox servers\n"
                "  • /server --connect <host:port> [user] [pass] - Connect to another server\n"
                "  • /server remotes - List connected remote servers\n"
                "  • /subagents - Manage subagents & automated helpers\n"
                "  • /subagents --help - Detailed subagents commands\n"
                "  • /subagents setup <nickname> <model> [role] - Register a subagent\n"
                "  • /subagents scripts - View or assign automation scripts\n"
                "  • /subagents --list - List all registered subagents\n"
                "  • /subagents rm <nickname> - Delete a subagent\n"
                "  • /subagents --global auto [nickname] - Auto-assign subagent globally across sessions\n"
                "  • /subagents auto [nickname] - Assign helper subagent to current session\n"
                "  • /chat save [name] - Save current conversation to session file\n"
                "  • /chat restore - Interactive session picker (press 'd' on any session to delete)\n"
                "  • /phantombox --routes - Show every cloud model returning 200 OK (fastest first)\n"
                "  • /phantombox --routes rescan - Re-ping all cloud models now and rebuild verified routes\n"
                "  • /phantombox --router - View Smart Router status, settings & verified routes\n"
                "  • /clear - Reset conversation\n"
                "  • /exit - Exit chat"
            )
            await self._add_system_message(help_text)
            return

        # Unknown slash command
        self._switch_to_chat()
        await self._add_system_message(f"Unknown command: {cmd}. Type /help for available commands.")

    async def _handle_settings_command(self, args: list[str]) -> None:
        if not args:
            self._open_settings_modal()
            return

        subcmd = args[0].lower()
        cfg = load_config()

        if subcmd == "mode" and len(args) > 1:
            target_mode = args[1].lower()
            if target_mode in ("restricted", "custom", "full_access"):
                set_config_val("permission_mode", target_mode)
                self._switch_to_chat()
                await self._add_system_message(f"Permission mode set to: [bold #f97316]{target_mode}[/bold #f97316]")
            else:
                self._switch_to_chat()
                await self._add_system_message("Invalid mode. Choose: restricted, custom, or full_access.")
            return

        if subcmd == "read" and len(args) > 1:
            path_val = args[1]
            set_config_val("allowed_read_paths", [path_val])
            self._switch_to_chat()
            await self._add_system_message(f"Allowed read scope set to: [bold]{path_val}[/bold]")
            return

        if subcmd == "write" and len(args) > 1:
            path_val = args[1]
            set_config_val("allowed_write_paths", [path_val])
            self._switch_to_chat()
            await self._add_system_message(f"Allowed write scope set to: [bold]{path_val}[/bold]")
            return

        if subcmd in ("search", "online", "internet"):
            if len(args) > 1:
                val_str = args[1].lower().strip()
                if val_str in ("on", "true", "yes", "1", "enable", "enabled"):
                    target_val = True
                elif val_str in ("off", "false", "no", "0", "disable", "disabled"):
                    target_val = False
                elif val_str in ("toggle",):
                    target_val = not cfg.get("enable_internet_search", True)
                else:
                    self._switch_to_chat()
                    await self._add_system_message(
                        f"Unknown search option '[bold]{val_str}[/bold]'. Use [cyan]/settings search on[/cyan] or [cyan]/settings search off[/cyan]."
                    )
                    return
            else:
                target_val = not cfg.get("enable_internet_search", True)
            set_config_val("enable_internet_search", target_val)
            self._sync_system_prompt()
            self._switch_to_chat()
            status_text = "[bold green]enabled[/bold green]" if target_val else "[bold red]disabled[/bold red]"
            await self._add_system_message(f"Online internet search is now {status_text}.")
            return

        self._open_settings_modal()

    async def _handle_thinking_toggle(self, val_str: str) -> None:
        self._switch_to_chat()
        cfg = load_config()
        cur = cfg.get("enable_thinking", False)
        clean = val_str.lower().strip()
        if clean in ("yes", "y", "on", "true", "1", "enable", "enabled"):
            target_val = True
        elif clean in ("no", "n", "off", "false", "0", "disable", "disabled"):
            target_val = False
        elif clean in ("", "toggle"):
            target_val = not cur
        else:
            await self._add_system_message(
                f"Invalid option '[bold]{clean}[/bold]'. Use [cyan]/models --thinking yes[/cyan] or [cyan]/models --thinking no[/cyan]."
            )
            return

        set_config_val("enable_thinking", target_val)
        self._sync_system_prompt()
        status_text = "[bold green]enabled[/bold green]" if target_val else "[bold red]disabled[/bold red]"
        await self._add_system_message(f"Model thinking / deep reasoning mode is now {status_text}.")

    async def _handle_mcp_command(self, args: list[str], raw_arg: str) -> None:
        self._switch_to_chat()
        if not args or args[0].lower() in ("--connect", "-c", "connect", "gui", "modal"):
            self._open_mcp_modal()
            return

        subcmd = args[0].lower()
        if subcmd == "list":
            connected = list_connected_providers()
            if not connected:
                await self._add_system_message(
                    "No external providers connected yet.\nUse [cyan]/mcp --connect[/cyan] to connect OpenRouter, Groq, Claude, OpenAI, Gemini, Qwen, etc."
                )
            else:
                lines = ["[bold]Connected External Providers:[/bold]"]
                for pid in connected:
                    p = get_provider(pid)
                    p_name = p["name"] if p else pid
                    lines.append(f"  • [green]●[/green] {p_name} ([dim]{pid}[/dim])")
                lines.append("\nSwitch models using [cyan]/models[/cyan].")
                await self._add_system_message("\n".join(lines))
            return

        if subcmd == "disconnect" and len(args) > 1:
            target_pid = args[1].lower()
            set_provider_key(target_pid, "")
            await self._add_system_message(f"Provider '[bold]{target_pid}[/bold]' disconnected.")
            return

        self._open_mcp_modal()

    def _open_mcp_modal(self) -> None:
        def on_closed(_) -> None:
            target_prompt = (
                self.query_one("#welcome-prompt", PromptArea)
                if self.in_welcome_mode
                else self.query_one("#dock-prompt", PromptArea)
            )
            target_prompt.focus()

        self.push_screen(McpConnectModal(theme_color=self.theme_color), on_closed)

    def _open_settings_modal(self) -> None:
        def on_closed(_) -> None:
            self._sync_system_prompt()
            target_prompt = (
                self.query_one("#welcome-prompt", PromptArea)
                if self.in_welcome_mode
                else self.query_one("#dock-prompt", PromptArea)
            )
            target_prompt.focus()

        self.push_screen(SettingsModal(theme_color=self.theme_color), on_closed)

    async def _handle_user_command(self, args: list[str], raw_arg: str) -> None:
        self._switch_to_chat()
        if not args:
            await self._add_system_message(
                f"Current user name: [bold {self.user_color}]{self.user_name}[/bold {self.user_color}].\n"
                f"Usage: /user <name> | /user --color <color> | /user --global <name> | /user --global --reset"
            )
            return

        # Check for --color flag
        if "--color" in args:
            idx = args.index("--color")
            if idx + 1 < len(args):
                color_val = args[idx + 1]
                if color_val == "--reset":
                    default_c = DEFAULT_CONFIG["user_color"]
                    self.user_color = default_c
                    set_config_val("user_color", default_c)
                    await self._add_system_message(
                        f"User display name color reset to default: [bold {default_c}]{default_c}[/bold {default_c}]."
                    )
                    return
                try:
                    parsed = Color.parse(color_val)
                    c_hex = parsed.hex6
                    self.user_color = c_hex
                    set_config_val("user_color", c_hex)
                    await self._add_system_message(
                        f"User display name color set to: [bold {c_hex}]{color_val} ({c_hex})[/bold {c_hex}]."
                    )
                    return
                except Exception:
                    await self._add_system_message(
                        f"[red]Invalid color '{color_val}'.[/red] Examples: cyan, #00ffcc, green, magenta, orange."
                    )
                    return
            else:
                await self._add_system_message(
                    f"Current user name color: [bold {self.user_color}]{self.user_color}[/bold {self.user_color}]. Usage: /user --color <color>"
                )
                return

        if "--reset" in args:
            new_val = reset_config_val("user_name")
            self.user_name = new_val or "User"
            await self._add_system_message(
                f"User name reset to default: [bold]{self.user_name}[/bold] (saved globally)."
            )
            return

        if "--global" in args:
            clean_parts = [a for a in args if a != "--global"]
            new_name = " ".join(clean_parts).strip()
            if not new_name:
                await self._add_system_message("Usage: /user --global <name>")
                return
            self.user_name = new_name
            set_config_val("user_name", new_name)
            await self._add_system_message(
                f"User name permanently saved globally: [bold {self.user_color}]{self.user_name}[/bold {self.user_color}]."
            )
            return

        # Session-only change
        self.user_name = raw_arg
        await self._add_system_message(
            f"User name updated for this session: [bold {self.user_color}]{self.user_name}[/bold {self.user_color}]."
        )

    async def _handle_assistant_command(self, args: list[str], raw_arg: str) -> None:
        self._switch_to_chat()
        if not args:
            await self._add_system_message(
                f"Current assistant name: [bold {self.assistant_color}]{self.assistant_name}[/bold {self.assistant_color}].\n"
                f"Usage: /assistant <name> | /assistant --color <color> | /assistant --global <name> | /assistant --global --reset"
            )
            return

        # Check for --color flag
        if "--color" in args:
            idx = args.index("--color")
            if idx + 1 < len(args):
                color_val = args[idx + 1]
                if color_val == "--reset":
                    default_c = DEFAULT_CONFIG["assistant_color"]
                    self.assistant_color = default_c
                    set_config_val("assistant_color", default_c)
                    await self._add_system_message(
                        f"Assistant display name color reset to default: [bold {default_c}]{default_c}[/bold {default_c}]."
                    )
                    return
                try:
                    parsed = Color.parse(color_val)
                    c_hex = parsed.hex6
                    self.assistant_color = c_hex
                    set_config_val("assistant_color", c_hex)
                    await self._add_system_message(
                        f"Assistant display name color set to: [bold {c_hex}]{color_val} ({c_hex})[/bold {c_hex}]."
                    )
                    return
                except Exception:
                    await self._add_system_message(
                        f"[red]Invalid color '{color_val}'.[/red] Examples: cyan, #f97316, purple, green, yellow."
                    )
                    return
            else:
                await self._add_system_message(
                    f"Current assistant name color: [bold {self.assistant_color}]{self.assistant_color}[/bold {self.assistant_color}]. Usage: /assistant --color <color>"
                )
                return

        if "--reset" in args:
            new_val = reset_config_val("assistant_name")
            self.assistant_name = new_val or "Assistant"
            await self._add_system_message(
                f"Assistant name reset to default: [bold]{self.assistant_name}[/bold] (saved globally)."
            )
            return

        if "--global" in args:
            clean_parts = [a for a in args if a != "--global"]
            new_name = " ".join(clean_parts).strip()
            if not new_name:
                await self._add_system_message("Usage: /assistant --global <name>")
                return
            self.assistant_name = new_name
            set_config_val("assistant_name", new_name)
            await self._add_system_message(
                f"Assistant name permanently saved globally: [bold {self.assistant_color}]{self.assistant_name}[/bold {self.assistant_color}]."
            )
            return

        # Session-only change
        self.assistant_name = raw_arg
        await self._add_system_message(
            f"Assistant name updated for this session: [bold {self.assistant_color}]{self.assistant_name}[/bold {self.assistant_color}]."
        )

    async def _handle_color_command(self, args: list[str], raw_arg: str) -> None:
        self._switch_to_chat()
        if not args:
            await self._add_system_message(
                f"Current border color: [bold {self.theme_color}]{self.theme_color}[/bold {self.theme_color}].\n"
                f"Usage: /color <name_or_hex> (e.g. /color cyan, /color #10b981) or /color --reset"
            )
            return

        if "--reset" in args:
            default_color = DEFAULT_CONFIG["theme_color"]
            self.theme_color = default_color
            set_config_val("theme_color", default_color)
            self._apply_border_color(default_color)
            await self._add_system_message(
                f"Border color reset to default: [bold {default_color}]{default_color}[/bold {default_color}]."
            )
            return

        # Parse and validate requested color
        chosen = args[0]
        try:
            parsed = Color.parse(chosen)
            color_hex = parsed.hex6
            self.theme_color = color_hex
            set_config_val("theme_color", color_hex)
            self._apply_border_color(color_hex)
            await self._add_system_message(
                f"Border color changed to: [bold {color_hex}]{chosen} ({color_hex})[/bold {color_hex}]."
            )
        except Exception:
            await self._add_system_message(
                f"[red]Invalid color '{chosen}'.[/red] Examples: cyan, orange, green, magenta, #3b82f6, #10b981."
            )

    async def _handle_server_command(self, args: list[str], raw_arg: str) -> None:
        """Handle /server commands (start, close, user, users, discoverable, --connect, --discover, remotes)."""
        self._switch_to_chat()
        from phantombox.server import (
            start_server,
            stop_server,
            is_server_running,
            set_user,
            remove_user,
            list_users,
            set_discoverable,
            is_discoverable,
            get_local_ip,
            get_server_config,
            discover_lan_servers,
            save_remote_server,
            list_remote_servers,
            remove_remote_server,
        )

        if not args:
            # Display current server status & info
            running = is_server_running()
            cfg = get_server_config()
            port = cfg.get("port", 8443)
            local_ip = get_local_ip()
            users = list_users()
            disc = is_discoverable()

            status_str = f"[bold green]ONLINE[/bold green] (listening on http://{local_ip}:{port})" if running else "[bold yellow]OFFLINE[/bold yellow]"
            disc_str = "[green]ENABLED[/green]" if disc else "[red]DISABLED[/red]"

            lines = [
                f"[bold cyan]PhantomBox AI Server Architecture[/bold cyan]",
                f"• Status: {status_str}",
                f"• Local IP (Hotspot/LAN): [bold]{local_ip}[/bold] (Port: {port})",
                f"• Network Discovery Beacon: {disc_str}",
                f"• Configured Users ({len(users)}):",
            ]
            if users:
                for u, priv in users.items():
                    priv_color = "red" if priv == "all" else ("yellow" if priv == "regular" else "blue")
                    lines.append(f"    - [bold]{u}[/bold]: [{priv_color}]{priv}[/{priv_color}] privilege")
            else:
                lines.append("    [dim](No users configured. Add one with: /server user <name> <password> <privilege>)[/dim]")

            lines.append("\n[bold]Server Commands:[/bold]")
            lines.append("  • [cyan]/server start [port][/cyan] - Open server for mobile/desktop connections")
            lines.append("  • [cyan]/server close[/cyan] - Close and shutdown server")
            lines.append("  • [cyan]/server user <name> <password> <all|regular|use-only>[/cyan] - Create/update user")
            lines.append("  • [cyan]/server user rm <name>[/cyan] - Remove user")
            lines.append("  • [cyan]/server discoverable <on|off>[/cyan] - Toggle phone discovery")
            lines.append("  • [cyan]/server --discover[/cyan] - Find other servers on WiFi/Hotspot")
            lines.append("  • [cyan]/server --connect <host:port> [user] [pass][/cyan] - Connect to another server")

            await self._add_system_message("\n".join(lines))
            return

        subcmd = args[0].lower()

        # 1. /server start [port]
        if subcmd == "start":
            port = None
            if len(args) > 1 and args[1].isdigit():
                port = int(args[1])
            success, msg = start_server(port=port)
            if success:
                cfg = get_server_config()
                p = cfg.get("port", 8443)
                ip = get_local_ip()
                await self._add_system_message(
                    f"[bold green]Server started successfully![/bold green]\n"
                    f"Address: [bold cyan]http://{ip}:{p}[/bold cyan]\n"
                    f"Mobile phones connected to this WiFi or Hotspot can now connect with authentication."
                )
            else:
                await self._add_system_message(f"[bold red]Failed to start server:[/bold red] {msg}")
            return

        # 2. /server close or /server stop
        if subcmd in ("close", "stop"):
            success, msg = stop_server()
            if success:
                await self._add_system_message(f"[bold yellow]Server closed.[/bold yellow] Incoming connections stopped.")
            else:
                await self._add_system_message(f"[yellow]{msg}[/yellow]")
            return

        # 3. /server user ...
        if subcmd == "user":
            if len(args) == 1:
                users = list_users()
                lines = [f"[bold cyan]Server Users ({len(users)}):[/bold cyan]"]
                for u, priv in users.items():
                    lines.append(f"  • [bold]{u}[/bold] - Privilege: {priv}")
                lines.append("\nUsage: /server user <name> <password> <all|regular|use-only>")
                await self._add_system_message("\n".join(lines))
                return

            if args[1].lower() in ("rm", "delete", "remove") and len(args) > 2:
                target_u = args[2]
                if remove_user(target_u):
                    await self._add_system_message(f"User '[bold]{target_u}[/bold]' deleted from server.")
                else:
                    await self._add_system_message(f"User '[bold]{target_u}[/bold]' not found.")
                return

            if len(args) >= 3:
                u_name = args[1]
                u_pw = args[2]
                u_priv = args[3].lower() if len(args) > 3 else "regular"
                if u_priv not in ("all", "regular", "use-only"):
                    await self._add_system_message(
                        f"[red]Invalid privilege '{u_priv}'.[/red] Options are: [bold]all[/bold], [bold]regular[/bold], or [bold]use-only[/bold]."
                    )
                    return

                set_user(u_name, u_pw, u_priv)
                priv_desc = {
                    "all": "Full control (modify server config, manage users, runtime settings, full chat)",
                    "regular": "Standard access (chat, scoped tools, custom UI settings, cannot modify server auth)",
                    "use-only": "Interact only (chat with models only, zero modification to any settings)",
                }
                await self._add_system_message(
                    f"[bold green]User saved successfully![/bold green]\n"
                    f"• Username: [bold]{u_name}[/bold]\n"
                    f"• Privilege: [bold cyan]{u_priv}[/bold cyan] ({priv_desc.get(u_priv, '')})"
                )
                return

            await self._add_system_message("Usage: [cyan]/server user <username> <password> <all|regular|use-only>[/cyan]")
            return

        # 4. /server users
        if subcmd == "users":
            users = list_users()
            lines = [f"[bold cyan]Server Users ({len(users)}):[/bold cyan]"]
            for u, priv in users.items():
                lines.append(f"  • [bold]{u}[/bold] - Privilege: {priv}")
            await self._add_system_message("\n".join(lines))
            return

        # 5. /server discoverable <on|off>
        if subcmd in ("discoverable", "discovery"):
            if len(args) > 1:
                opt = args[1].lower().strip()
                if opt in ("on", "true", "yes", "1", "enable"):
                    set_discoverable(True)
                    await self._add_system_message("Server discovery beacon: [bold green]ENABLED[/bold green]. Mobile devices on WiFi/Hotspot can auto-discover this server.")
                elif opt in ("off", "false", "no", "0", "disable"):
                    set_discoverable(False)
                    await self._add_system_message("Server discovery beacon: [bold red]DISABLED[/bold red]. Server is hidden from network broadcast.")
                else:
                    await self._add_system_message("Usage: /server discoverable <on|off>")
            else:
                cur = is_discoverable()
                await self._add_system_message(f"Server discovery is currently: {'[green]ON[/green]' if cur else '[red]OFF[/red]'}")
            return

        # 6. /server --discover (scan LAN for other PhantomBox servers)
        if subcmd in ("--discover", "-d", "discover", "scan"):
            await self._add_system_message("Scanning WiFi / Hotspot network for PhantomBox servers (UDP 8444)...")
            found = discover_lan_servers(timeout_seconds=2.0)
            if not found:
                await self._add_system_message("No other PhantomBox servers detected on this network.")
            else:
                lines = [f"[bold green]Found {len(found)} server(s) on network:[/bold green]"]
                for s in found:
                    lines.append(f"  • [bold]{s.get('server_name')}[/bold] at [cyan]{s.get('url')}[/cyan] (IP: {s.get('ip')}:{s.get('port')})")
                lines.append("\nConnect using: /server --connect <host:port> [user] [password]")
                await self._add_system_message("\n".join(lines))
            return

        # 7. /server --connect <host[:port]> [username] [password]
        if subcmd in ("--connect", "-c", "connect"):
            if len(args) < 2:
                remotes = list_remote_servers()
                if not remotes:
                    await self._add_system_message("Usage: [cyan]/server --connect <host:port> <username> <password> [server_name][/cyan]")
                else:
                    lines = ["[bold cyan]Connected Remote Servers:[/bold cyan]"]
                    for name, info in remotes.items():
                        lines.append(f"  • [bold]{name}[/bold] -> {info.get('url')} (User: {info.get('username')})")
                    lines.append("\nUse models with: [cyan]/models[/cyan] or type [cyan]/models server/<name>/<model>[/cyan]")
                    await self._add_system_message("\n".join(lines))
                return

            target = args[1].replace("http://", "").replace("https://", "")
            if ":" in target:
                host, port_str = target.split(":", 1)
                port = int(port_str) if port_str.isdigit() else 8443
            else:
                host = target
                port = 8443

            user = args[2] if len(args) > 2 else "admin"
            password = args[3] if len(args) > 3 else ""
            srv_alias = args[4] if len(args) > 4 else f"{host.replace('.', '_')}_{port}"

            await self._add_system_message(f"Connecting and verifying credentials on remote server [cyan]{host}:{port}[/cyan]...")

            # Validate remote server connection via HTTP request
            import httpx
            auth_h = "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("utf-8")
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"http://{host}:{port}/v1/models",
                        headers={"Authorization": auth_h},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        remote_models = [m.get("id") for m in data.get("data", [])]
                        save_remote_server(srv_alias, host, port, user, password)
                        # Store cached models in remote server entry
                        cfg = get_server_config()
                        if srv_alias in cfg.get("remote_servers", {}):
                            cfg["remote_servers"][srv_alias]["cached_models"] = remote_models
                            from phantombox.server import save_server_config
                            save_server_config(cfg)

                        await self._add_system_message(
                            f"[bold green]Connected successfully to remote server '{srv_alias}'![/bold green]\n"
                            f"Discovered {len(remote_models)} model(s): {', '.join(remote_models[:5])}...\n"
                            f"Type [cyan]/models[/cyan] to switch to models on this remote server."
                        )
                    elif resp.status_code == 401:
                        await self._add_system_message(f"[bold red]Authentication failed[/bold red] on {host}:{port}. Check username and password.")
                    else:
                        await self._add_system_message(f"[red]Remote server replied with status {resp.status_code}:[/red] {resp.text}")
            except Exception as e:
                await self._add_system_message(f"[bold red]Could not reach server at {host}:{port}:[/bold red] {e}")
            return

        # 8. /server remotes
        if subcmd == "remotes":
            remotes = list_remote_servers()
            if not remotes:
                await self._add_system_message("No remote servers connected. Connect with [cyan]/server --connect <host:port> <user> <pass>[/cyan]")
            else:
                lines = [f"[bold cyan]Connected Remote Servers ({len(remotes)}):[/bold cyan]"]
                for name, info in remotes.items():
                    cached = info.get("cached_models", [])
                    lines.append(f"  • [bold]{name}[/bold] ({info.get('url')}) - Models: {len(cached)}")
                await self._add_system_message("\n".join(lines))
            return

        await self._add_system_message(
            f"Unknown server subcommand '[bold]{subcmd}[/bold]'.\n"
            f"Options: [cyan]start, close, user, users, discoverable, --connect, --discover, remotes[/cyan]"
        )

    async def _handle_subagents_command(self, args: list[str], raw_arg: str) -> None:
        """Handle /subagents commands (help, setup, scripts, --list, rm, --global auto, auto)."""
        self._switch_to_chat()
        from phantombox.subagents import (
            register_subagent,
            remove_subagent,
            list_subagents,
            get_subagent,
            set_global_auto,
            get_global_auto,
            AVAILABLE_SCRIPTS,
        )

        if not args or args[0].lower() in ("--help", "-h", "help"):
            help_lines = [
                "[bold cyan]PhantomBox Subagents Command Guide[/bold cyan]",
                "• [cyan]/subagents[/cyan] or [cyan]/subagents --help[/cyan] - Show this command reference",
                "• [cyan]/subagents setup <nickname> <model> [role][/cyan] - Register a subagent (local or cloud model)",
                "• [cyan]/subagents scripts[/cyan] - View built-in automation scripts (suggestions, web_assistant, code_reviewer...)",
                "• [cyan]/subagents scripts <nickname> <script1,script2>[/cyan] - Assign scripts to a subagent",
                "• [cyan]/subagents --list[/cyan] (or [cyan]/subagents list[/cyan]) - Show all registered subagents",
                "• [cyan]/subagents rm <nickname>[/cyan] - Delete a subagent",
                "• [cyan]/subagents --global auto <nickname>[/cyan] - Automatically assign subagent when starting chat",
                "• [cyan]/subagents --global auto off[/cyan] - Turn off global auto subagent",
                "• [cyan]/subagents auto <nickname>[/cyan] - Assign helper subagent to current chat session",
                "• [cyan]/subagents auto off[/cyan] - Remove helper subagent from current chat session",
                "\n[dim]Note: Active models can also autonomously invoke subagents via the `call_subagent` tool.[/dim]",
            ]
            await self._add_system_message("\n".join(help_lines))
            return

        subcmd = args[0].lower()

        # 1. /subagents --list or /subagents list
        if subcmd in ("--list", "-l", "list"):
            agents = list_subagents()
            if not agents:
                await self._add_system_message(
                    "No subagents registered yet.\n"
                    "Create one using: [cyan]/subagents setup <nickname> <model> [role][/cyan]"
                )
                return

            g_auto, g_nick = get_global_auto()
            lines = [f"[bold cyan]Registered Subagents ({len(agents)}):[/bold cyan]"]
            for nick, info in agents.items():
                is_sess = " [green][SESSION ACTIVE][/green]" if self.session_subagent == nick else ""
                is_glob = " [yellow][GLOBAL AUTO][/yellow]" if g_auto and g_nick == nick else ""
                scripts_str = ", ".join(info.get("scripts", [])) if info.get("scripts") else "none"
                lines.append(
                    f"  • [bold]{nick}[/bold] -> model: [cyan]{info.get('model')}[/cyan] ({info.get('role', 'Helper')}){is_sess}{is_glob}\n"
                    f"    Scripts: [dim]{scripts_str}[/dim]"
                )
            await self._add_system_message("\n".join(lines))
            return

        # 2. /subagents setup <nickname> <model> [role]
        if subcmd == "setup":
            if len(args) < 3:
                await self._add_system_message(
                    "Usage: [cyan]/subagents setup <nickname> <model> [role][/cyan]\n"
                    "Example: [dim]/subagents setup reviewer qwen2.5-coder-7b 'Code Auditor'[/dim]\n"
                    "Example: [dim]/subagents setup researcher openrouter/deepseek-r1 'Web Researcher'[/dim]"
                )
                return

            nick = args[1]
            model = args[2]
            role = " ".join(args[3:]) if len(args) > 3 else "Assistant Helper"

            # Guard: phantombox/subagents must NOT be set up through /subagents setup.
            # /subagents is already tailored for provisioning subagents — selecting
            # 'phantombox/subagents' as a subagent's model would recurse (the router
            # would spawn subagents inside a subagent). Users who want automatic
            # model discovery should switch to 'phantombox/router' as their model instead.
            clean_model = model.strip().lower()
            if "phantombox/subagents" in clean_model or clean_model in ("subagents", "phantombox:subagents", "phantombox/subagents"):
                await self._add_system_message(
                    "[bold red]Not allowed:[/bold red] A subagent cannot use 'phantombox/subagents' as its model "
                    "(that would create subagents inside subagents).\n"
                    "• Use a concrete model instead, e.g. [cyan]/subagents setup helper qwen2.5-coder-7b[/cyan]\n"
                    "• If you want automatic discovery of available model responses, switch your main model to "
                    "[cyan]phantombox/router[/cyan] — it is tailored for that purpose."
                )
                return

            ok, msg = register_subagent(nick, model, role=role)
            if ok:
                self._sync_system_prompt()
                await self._add_system_message(
                    f"[bold green]Subagent registered successfully![/bold green]\n"
                    f"• Nickname: [bold]{nick}[/bold]\n"
                    f"• Model: [cyan]{model}[/cyan]\n"
                    f"• Role: {role}\n"
                    f"You or the active model can now call this subagent, or run [cyan]/subagents auto {nick}[/cyan] to attach it."
                )
            else:
                await self._add_system_message(f"[bold red]Failed to register subagent:[/bold red] {msg}")
            return

        # 3. /subagents scripts
        if subcmd == "scripts":
            if len(args) == 1:
                lines = ["[bold cyan]Available Subagent Automation Scripts:[/bold cyan]"]
                for s_id, s_info in AVAILABLE_SCRIPTS.items():
                    lines.append(f"  • [bold]{s_id}[/bold]: {s_info['name']}\n    [dim]{s_info['description']}[/dim]")
                lines.append("\nAssign to subagent using: [cyan]/subagents scripts <nickname> <script1,script2>[/cyan]")
                await self._add_system_message("\n".join(lines))
                return

            nick = args[1].lower()
            agent = get_subagent(nick)
            if not agent:
                await self._add_system_message(f"[red]Subagent '{nick}' not found.[/red] Check with /subagents --list.")
                return

            if len(args) > 2:
                script_list = [s.strip().lower() for s in args[2].split(",") if s.strip()]
                register_subagent(nick, agent["model"], role=agent.get("role", "Helper"), scripts=script_list)
                await self._add_system_message(
                    f"Updated scripts for subagent '[bold]{nick}[/bold]': {', '.join(script_list) if script_list else 'none'}"
                )
                return

            cur_scripts = agent.get("scripts", [])
            await self._add_system_message(
                f"Subagent '[bold]{nick}[/bold]' active scripts: {', '.join(cur_scripts) if cur_scripts else 'none'}\n"
                f"Update with: [cyan]/subagents scripts {nick} suggestions,web_assistant[/cyan]"
            )
            return

        # 4. /subagents rm <nickname>
        if subcmd in ("rm", "delete", "remove"):
            if len(args) < 2:
                await self._add_system_message("Usage: [cyan]/subagents rm <nickname>[/cyan]")
                return
            nick = args[1].lower()
            ok, msg = remove_subagent(nick)
            if self.session_subagent == nick:
                self.session_subagent = None
            if ok:
                self._sync_system_prompt()
                await self._add_system_message(f"[bold yellow]{msg}[/bold yellow]")
            else:
                await self._add_system_message(f"[red]{msg}[/red]")
            return

        # 5. /subagents --global auto [nickname|off]
        if subcmd == "--global":
            if len(args) >= 3 and args[1].lower() == "auto":
                target_val = args[2].lower()
                if target_val in ("off", "false", "0", "disable"):
                    set_global_auto(False)
                    await self._add_system_message("Global auto-subagent disabled.")
                else:
                    ok, msg = set_global_auto(True, target_val)
                    if ok:
                        await self._add_system_message(f"[bold green]{msg}[/bold green]")
                    else:
                        await self._add_system_message(f"[red]{msg}[/red]")
                return
            await self._add_system_message("Usage: [cyan]/subagents --global auto <nickname>[/cyan] or [cyan]/subagents --global auto off[/cyan]")
            return

        # 6. /subagents auto [nickname|off]
        if subcmd == "auto":
            if len(args) > 1:
                opt = args[1].lower()
                if opt in ("off", "none", "clear", "disable"):
                    self.session_subagent = None
                    await self._add_system_message("Session helper subagent removed.")
                    return
                agent = get_subagent(opt)
                if not agent:
                    await self._add_system_message(f"[red]Subagent '{opt}' not found.[/red] Set it up first using /subagents setup.")
                    return
                self.session_subagent = opt
                await self._add_system_message(
                    f"Subagent [bold cyan]{opt}[/bold cyan] ({agent.get('role', 'Helper')}) is now actively assisting this session!"
                )
                return
            else:
                if self.session_subagent:
                    await self._add_system_message(f"Current active session subagent: [bold cyan]{self.session_subagent}[/bold cyan]")
                else:
                    await self._add_system_message("No subagent currently attached to this session. Attach one with [cyan]/subagents auto <nickname>[/cyan]")
                return

        await self._add_system_message(
            f"Unknown subagents subcommand '{subcmd}'. Type [cyan]/subagents --help[/cyan] for available commands."
        )

    async def _handle_chat_command(self, args: list[str], raw_arg: str) -> None:
        """Handle /chat save [name] and /chat restore."""
        self._switch_to_chat()
        from phantombox.sessions import (
            save_chat_session,
            list_saved_sessions,
            load_chat_session,
        )

        if not args:
            await self._add_system_message(
                "Chat Session Commands:\n"
                "  • [cyan]/chat save [name][/cyan] - Save current chat session\n"
                "  • [cyan]/chat restore[/cyan] - Open interactive saved session dialog (press 'd' to delete)"
            )
            return

        subcmd = args[0].lower()

        # 1. /chat save [name]
        if subcmd == "save":
            sess_name = " ".join(args[1:]).strip() if len(args) > 1 else f"session_{self.model_name}"
            # Exclude system prompt from count calculation
            user_asst_msgs = [m for m in self.messages if m.get("role") != "system"]
            if not user_asst_msgs:
                await self._add_system_message("Cannot save empty conversation. Send at least one message first.")
                return

            sess_id = save_chat_session(sess_name, self.model_name, self.messages)
            await self._add_system_message(
                f"[bold green]Chat session saved successfully![/bold green]\n"
                f"• Name: [bold]{sess_name}[/bold]\n"
                f"• ID: [dim]{sess_id}[/dim]\n"
                f"• Messages: {len(user_asst_msgs)}\n"
                f"You can restore it anytime with [cyan]/chat restore[/cyan]."
            )
            return

        # 2. /chat restore
        if subcmd in ("restore", "load", "open"):
            saved = list_saved_sessions()
            if not saved:
                await self._add_system_message(
                    "No saved chat sessions found. Save one using [cyan]/chat save <name>[/cyan]."
                )
                return

            def on_session_picked(result: str | None) -> None:
                target_prompt = (
                    self.query_one("#welcome-prompt", PromptArea)
                    if self.in_welcome_mode
                    else self.query_one("#dock-prompt", PromptArea)
                )
                target_prompt.focus()

                if not result:
                    return

                if result.startswith("restore:"):
                    sess_id = result.split(":", 1)[1]
                    self.run_worker(self._restore_session_worker(sess_id))

            self.push_screen(SessionPickerModal(saved, theme_color=self.theme_color), on_session_picked)
            return

        await self._add_system_message(
            f"Unknown /chat subcommand '{subcmd}'. Use [cyan]/chat save [name][/cyan] or [cyan]/chat restore[/cyan]."
        )

    async def _restore_session_worker(self, session_id: str) -> None:
        """Worker to restore conversation messages into chat view."""
        from phantombox.sessions import load_chat_session
        data = load_chat_session(session_id)
        if not data:
            await self._add_system_message(f"[red]Could not load session '{session_id}'.[/red]")
            return

        restored_messages = data.get("messages", [])
        if not restored_messages:
            await self._add_system_message(f"[yellow]Session '{session_id}' has no messages.[/yellow]")
            return

        self.messages = restored_messages
        if data.get("model"):
            self.model_name = data["model"]
            self._update_status_model()

        # Re-render chat view
        chat_view = self.query_one("#chat-view")
        await chat_view.remove_children()
        self._switch_to_chat()

        for m in self.messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "user":
                await chat_view.mount(ChatMessage(self.user_name, content, "user", sender_color=self.user_color))
            elif role == "assistant":
                if content:
                    await chat_view.mount(ChatMessage(self.assistant_name, content, "assistant", sender_color=self.assistant_color))
            elif role == "tool":
                await chat_view.mount(ChatMessage("Tool", content[:300] + ("..." if len(content) > 300 else ""), "tool"))

        chat_view.scroll_end(animate=False)
        await self._add_system_message(
            f"[bold green]Session '{data.get('name', session_id)}' restored successfully![/bold green] "
            f"({len(self.messages)} messages, Model: {self.model_name})"
        )

    async def _handle_phantombox_command(self, args: list[str], raw_arg: str) -> None:
        """Handle /phantombox commands (/phantombox --router, --routes, etc.)."""
        self._switch_to_chat()
        from phantombox.router import (
            get_router_config,
            save_router_config,
            get_verified_models,
            clear_router_history,
        )

        # ---- /phantombox --routes : scan every cloud model once, keep only 200 OK routes ----
        if args and args[0].lower() in ("--routes", "-rt", "routes"):
            from phantombox.router import scan_all_routes

            force_rescan = len(args) > 1 and args[1].lower() in ("--rescan", "rescan", "refresh")
            verified = get_verified_models()

            if force_rescan or not verified:
                # Animated "Scanning Routes" spinner while the full probe runs
                spinner = SpinnerThinking(color="#38bdf8", label_text="Scanning Routes…")
                try:
                    chat_view = self.query_one("#chat-view", VerticalScroll)
                    await chat_view.mount(spinner)
                    chat_view.scroll_end(animate=False)
                except Exception:
                    spinner = None

                try:
                    verified = await scan_all_routes()
                finally:
                    if spinner:
                        try:
                            await spinner.remove()
                        except Exception:
                            pass
            else:
                # Instantly show the last scan; a background re-scan refreshes silently
                await self._add_system_message(
                    "[dim]Showing last verified routes. Add [cyan]rescan[/cyan] to force a fresh ping of every model."
                )

            if not verified:
                await self._add_system_message(
                    "[yellow]No working routes found. Check that your provider API keys have valid quota via [cyan]/mcp --connect[/cyan].[/yellow]"
                )
                return

            lines = [
                "[bold cyan]PhantomBox Verified Routes[/bold cyan] "
                f"({len(verified)} models returning 200 OK, fastest first):",
            ]
            for v in verified:
                lat = v.get("last_latency", 0)
                lat_str = f"{lat:.2f}s" if lat >= 1.0 else f"{int(lat*1000)}ms"
                lines.append(f"  • [bold]{v.get('provider')}/{v.get('model_id')}[/bold] — [green]{lat_str}[/green]")
            lines.append(
                "\n[dim]Switch to phantombox/router in /models to tap these routes instantly. "
                "Routes refresh via /phantombox --routes rescan, or automatically scraped on error.[/dim]"
            )
            await self._add_system_message("\n".join(lines))
            return

        if not args or args[0].lower() in ("--router", "-r", "router"):
            cfg = get_router_config()
            verified = get_verified_models()

            lines = [
                "[bold cyan]PhantomBox Autonomous Smart Router Status[/bold cyan]",
                f"• Active Model Identifier: [bold #f97316]phantombox/router[/bold #f97316]",
                f"• Strategy: [green]{cfg.get('strategy', 'adaptive')}[/green] (prioritize proven working models)",
                f"• Exploratory Attempts: [yellow]{cfg.get('max_exploratory_attempts', 10)}[/yellow] (tests up to 10 random models before guaranteed fallback)",
                f"• Probe Timeout per Model: [dim]{cfg.get('timeout_per_attempt', 6.0)}s[/dim]",
                f"• Verified 200 OK Models ({len(verified)}):",
            ]
            if verified:
                for v in verified[:8]:
                    p = v.get("provider")
                    m = v.get("model_id")
                    count = v.get("success_count", 1)
                    lat = v.get("last_latency", 0)
                    lines.append(f"    - [bold]{p}/{m}[/bold] (pings: {count}, last: {lat}s)")
            else:
                lines.append("    [dim](No models benchmarked yet. Switch to 'phantombox/router' in /models to start auto-routing!)[/dim]")

            lines.append("\n[bold]Router Commands:[/bold]")
            lines.append("  • [cyan]/phantombox --routes[/cyan] - Show every cloud model currently returning 200 OK (fastest first)")
            lines.append("  • [cyan]/phantombox --routes rescan[/cyan] - Re-ping all cloud models now and rebuild the verified route table")
            lines.append("  • [cyan]/models phantombox/router[/cyan] - Switch active chat model to Smart Router (taps verified routes instantly, re-scans in background after each reply)")
            lines.append("  • [cyan]/phantombox --router clear[/cyan] - Reset verified routes history")
            lines.append("  • [cyan]/phantombox --router timeout <seconds>[/cyan] - Set per-model probe timeout")

            # Check if sub-actions were passed (e.g. /phantombox --router clear)
            if len(args) > 1:
                subaction = args[1].lower()
                if subaction == "clear":
                    clear_router_history()
                    await self._add_system_message("[yellow]Router verified models cache cleared successfully.[/yellow]")
                    return
                elif subaction in ("attempts", "max") and len(args) > 2 and args[2].isdigit():
                    new_val = int(args[2])
                    cfg["max_exploratory_attempts"] = new_val
                    save_router_config(cfg)
                    await self._add_system_message(f"Max exploratory attempts set to: [bold cyan]{new_val}[/bold cyan]")
                    return
                elif subaction in ("timeout", "t") and len(args) > 2:
                    try:
                        new_t = float(args[2])
                        cfg["timeout_per_attempt"] = new_t
                        save_router_config(cfg)
                        await self._add_system_message(f"Probe timeout per model set to: [bold cyan]{new_t}s[/bold cyan]")
                        return
                    except Exception:
                        pass

            await self._add_system_message("\n".join(lines))
            return

        await self._add_system_message(
            "Usage:\n"
            "  • [cyan]/phantombox --routes[/cyan] - Show verified 200 OK routes (add [cyan]rescan[/cyan] to re-ping every model now)\n"
            "  • [cyan]/phantombox --router[/cyan] - View or configure the Smart Router."
        )

    def _get_all_available_models(self) -> list[tuple[str, str, bool]]:
        """Collect local downloaded models on disk AND models from connected cloud providers."""
        downloaded_paths = list_downloaded_models()
        running = list_running()

        options: list[tuple[str, str, bool]] = []
        seen = set()

        # 1. Local downloaded models
        for p in downloaded_paths:
            fname = p.name.lower()
            stem = p.stem.lower()
            matched_key = None
            for k, info in RECOMMENDED_MODELS.items():
                if info["filename"].lower() == fname or k.lower() in stem:
                    matched_key = k
                    break

            canonical_name = matched_key if matched_key else p.stem
            if canonical_name in seen:
                continue
            seen.add(canonical_name)

            is_current = (
                canonical_name == self.model_name
                or stem == self.model_name.lower()
            )
            is_running = (canonical_name in running or stem in running)

            port_str = ""
            if is_running and canonical_name in running:
                port_str = f" (port {running[canonical_name].get('port', 8080)})"
            curr_str = " [CURRENT]" if is_current else ""
            run_dot = "●" if is_running else "○"

            label = f"{run_dot} [LOCAL] {canonical_name}{port_str}{curr_str}"
            options.append((canonical_name, label, is_running))

        # PhantomBox Smart Router Entry (Priority top entry)
        is_router_current = (self.model_name in ("phantombox/router", "router"))
        r_curr_str = " [CURRENT]" if is_router_current else ""
        options.append(("phantombox/router", f"● [ROUTER] phantombox/router (Auto Router){r_curr_str}", True))

        # PhantomBox Subagents Mode Entry
        is_sub_current = (self.model_name in ("phantombox/subagents", "subagents"))
        s_curr_str = " [CURRENT]" if is_sub_current else ""
        options.append(("phantombox/subagents", f"● [AGENTS] phantombox/subagents (Auto Router + Helper Subagent){s_curr_str}", True))

        # 2. Cloud MCP / API provider models
        connected_pids = list_connected_providers()
        for pid in connected_pids:
            if pid == "phantombox":
                continue
            p_info = get_provider(pid)
            if not p_info:
                continue
            models_list = fetch_cached_or_default_models(pid)
            tag = p_info.get("tag", p_info.get("name", pid))
            for mid in models_list:
                full_model_id = f"{pid}:{mid}"
                is_current = (
                    full_model_id == self.model_name
                    or f"{pid}/{mid}" == self.model_name
                    or (pid == "anthropic" and f"claude/{mid}" == self.model_name)
                )
                curr_str = " [CURRENT]" if is_current else ""
                label = f"● [{tag}] {mid}{curr_str}"
                options.append((full_model_id, label, True))

        # 3. Connected Remote PhantomBox Servers (/server --connect)
        from phantombox.server import list_remote_servers
        remote_servers = list_remote_servers()
        for srv_name, srv_conf in remote_servers.items():
            cached_models = srv_conf.get("cached_models", ["default"])
            for r_mid in cached_models:
                full_model_id = f"server/{srv_name}/{r_mid}"
                is_current = (full_model_id == self.model_name)
                curr_str = " [CURRENT]" if is_current else ""
                label = f"● [SERVER:{srv_name}] {r_mid}{curr_str}"
                options.append((full_model_id, label, True))

        return options

    def _open_models_picker(self) -> None:
        """Show an interactive modal picker to choose among local & connected cloud models."""
        options = self._get_all_available_models()

        def on_selected(choice: str | None) -> None:
            target_prompt = (
                self.query_one("#welcome-prompt", PromptArea)
                if self.in_welcome_mode
                else self.query_one("#dock-prompt", PromptArea)
            )
            target_prompt.focus()

            if choice and choice != self.model_name:
                self.run_worker(self._switch_to_model(choice))

        self.push_screen(ModelPickerModal(self.model_name, options, theme_color=self.theme_color), on_selected)

    async def _switch_to_model(self, target_name: str) -> None:
        """Switch active model, supporting local llama servers and connected cloud providers."""
        self._switch_to_chat()
        target_key = target_name.strip()

        # Check if target is a remote provider model
        if target_key in ("phantombox/router", "router"):
            self.model_name = "phantombox/router"
            self._update_status_model()
            await self._add_system_message(
                "Switched to [bold #f97316]PhantomBox Smart Router[/bold #f97316].\n"
                "The router will automatically probe connected cloud providers until a 200 OK response is found."
            )
            return

        if target_key in ("phantombox/subagents", "subagents"):
            self.model_name = "phantombox/subagents"
            self.session_subagent = "subagent1"
            self._update_status_model()
            await self._add_system_message(
                "Switched to [bold #f97316]PhantomBox Subagents Mode[/bold #f97316].\n"
                "In this mode, the Smart Router executes prompts and dynamically spins up helper subagent 'subagent1' (with suggestions) using responsive 200 OK pings."
            )
            return

        p_info, raw_mid = parse_model_identifier(target_key)
        if not p_info:
            # Check if target matches a model from any connected provider (e.g. user typed /models gpt-4o)
            for pid in list_connected_providers():
                cached = fetch_cached_or_default_models(pid)
                if target_key in cached or target_key.lower() in [m.lower() for m in cached]:
                    p_info = get_provider(pid)
                    raw_mid = target_key
                    target_key = f"{pid}:{raw_mid}"
                    break

        if p_info:
            pid = p_info["id"]
            if not is_provider_connected(pid):
                await self._add_system_message(
                    f"[yellow]Provider '{p_info['name']}' is not connected.[/yellow] "
                    f"Run [cyan]/mcp --connect[/cyan] to connect your API key."
                )
                return

            self.model_name = target_key
            self._update_status_model()
            display_name = format_model_display_name(self.model_name)
            await self._add_system_message(
                f"Switched to remote model [bold #f97316]{display_name}[/bold #f97316] via [cyan]{p_info['name']}[/cyan]."
            )
            return

        # Local model logic
        running = list_running()
        if target_key in running:
            self.model_name = target_key
            self.port = running[target_key]["port"]
            self._update_status_model()
            await self._add_system_message(
                f"Switched to running model [bold #f97316]{self.model_name}[/bold #f97316] on port {self.port}."
            )
            return

        # Attempt to auto-start if already downloaded
        await self._add_system_message(f"Starting model server for [bold]{target_key}[/bold]...")
        used_ports = [info.get("port", 8080) for info in running.values()]
        new_port = 8080
        while new_port in used_ports:
            new_port += 1

        success = start_model(target_key, port=new_port)
        if success:
            running = list_running()
            if target_key in running:
                self.model_name = target_key
                self.port = running[target_key]["port"]
                self._update_status_model()
                await self._add_system_message(
                    f"Model [bold #f97316]{self.model_name}[/bold #f97316] started and connected on port {self.port}."
                )
                return

        await self._add_system_message(
            f"[red]Could not start '{target_key}'.[/red] "
            f"Please ensure it is downloaded or connect a provider with [cyan]/mcp --connect[/cyan]."
        )

    async def _send_user_message(self, text: str) -> None:
        if self._is_generating:
            self._stop_requested = True
            if self._current_thinking_container:
                try:
                    await self._current_thinking_container.remove()
                except Exception:
                    pass
                self._current_thinking_container = None
            await self._add_system_message("[dim italic]Previous response interrupted by new message.[/dim italic]")

        self._stop_requested = False
        self._is_generating = True

        # Clear any pinned router model from previous turns so new user message gets a fresh optimal route
        from phantombox.providers import clear_pinned_router_model
        clear_pinned_router_model()

        self.messages.append({"role": "user", "content": text})
        self._update_status_model()
        chat_view = self.query_one("#chat-view")

        # Mount user message bubble with customized user color
        user_msg = ChatMessage(self.user_name, text, "user", sender_color=self.user_color)
        await chat_view.mount(user_msg)
        chat_view.scroll_end(animate=False)

        # Show thinking indicator with spinner using assistant color
        asst_header = Static(f"● {self.assistant_name}", classes="message-header")
        if self.assistant_color:
            asst_header.styles.color = self.assistant_color

        thinking_container = Container(
            asst_header,
            SpinnerThinking(color=self.assistant_color),
            id="thinking-row",
            classes="message-row role-assistant",
        )
        self._current_thinking_container = thinking_container
        await chat_view.mount(thinking_container)
        chat_view.scroll_end(animate=False)

        # Launch streaming worker
        self._stream_completion(thinking_container)

    @work(exclusive=True)
    async def _stream_completion(self, thinking_container: Container) -> None:
        """Stream completion from llama server in a background worker, handling tool calls."""
        chat_view = self.query_one("#chat-view")

        loop_count = 0
        max_tool_loops = 5

        while loop_count < max_tool_loops:
            if self._stop_requested:
                break
            loop_count += 1
            full_reply = ""
            current_assistant_msg: ChatMessage | None = None
            first_token = True
            tool_calls_dict: Dict[int, Dict[str, Any]] = {}

            try:
                async for delta in stream_chat_deltas(
                    self.model_name,
                    self.port,
                    self.messages,
                    get_tool_definitions(),
                ):
                    if self._stop_requested:
                        break
                    # Handle tool calls stream chunk
                    raw_tool_calls = delta.get("tool_calls")
                    if raw_tool_calls:
                        for tc_delta in raw_tool_calls:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_dict:
                                tool_calls_dict[idx] = {
                                    "id": tc_delta.get("id") or f"call_{idx}",
                                    "type": "function",
                                    "function": {
                                        "name": tc_delta.get("function", {}).get("name", ""),
                                        "arguments": "",
                                    },
                                }
                            else:
                                if tc_delta.get("id"):
                                    tool_calls_dict[idx]["id"] = tc_delta["id"]
                                if tc_delta.get("function", {}).get("name"):
                                    tool_calls_dict[idx]["function"]["name"] += tc_delta["function"]["name"]

                            # Preserve thought_signature and extra fields from Gemini / OpenAI-compat tools
                            for extra_k in ("thought_signature", "thoughtSignature", "extra_content"):
                                if tc_delta.get(extra_k):
                                    tool_calls_dict[idx][extra_k] = tc_delta[extra_k]
                                if tc_delta.get("function", {}).get(extra_k):
                                    tool_calls_dict[idx]["function"][extra_k] = tc_delta["function"][extra_k]

                            arg_chunk = tc_delta.get("function", {}).get("arguments", "")
                            if arg_chunk:
                                tool_calls_dict[idx]["function"]["arguments"] += arg_chunk
                        continue

                    # Handle content tokens stream chunk
                    content_chunk = delta.get("content")
                    if content_chunk:
                        if first_token:
                            await thinking_container.remove()
                            first_token = False
                            current_assistant_msg = ChatMessage(
                                self.assistant_name,
                                "",
                                "assistant",
                                sender_color=self.assistant_color,
                            )
                            await chat_view.mount(current_assistant_msg)

                            # Keep an active streaming indicator spinner directly below message so users see ongoing stream / lag
                            stream_spinner = SpinnerThinking(color=self.assistant_color, label_text="Generating…", id="active-stream-spinner")
                            await chat_view.mount(stream_spinner)
                            self._current_thinking_container = stream_spinner

                        full_reply += content_chunk
                        if current_assistant_msg:
                            current_assistant_msg.update_content(full_reply)
                        chat_view.scroll_end(animate=False)

                # Remove the active stream spinner once the model finishes streaming this chunk
                if self._current_thinking_container and self._current_thinking_container.id == "active-stream-spinner":
                    try:
                        await self._current_thinking_container.remove()
                    except Exception:
                        pass
                    self._current_thinking_container = None

                if self._stop_requested:
                    if full_reply:
                        self.messages.append({"role": "assistant", "content": full_reply})
                    self._is_generating = False
                    self._current_thinking_container = None
                    self._update_status_model()
                    break

                # End of stream chunk processing
                # If tool calls were made by model:
                if tool_calls_dict:
                    if self._stop_requested:
                        self._is_generating = False
                        self._current_thinking_container = None
                        self._update_status_model()
                        break
                    try:
                        await thinking_container.remove()
                    except Exception:
                        pass

                    calls_list = list(tool_calls_dict.values())
                    self.messages.append({
                        "role": "assistant",
                        "content": full_reply or None,
                        "tool_calls": calls_list,
                    })

                    # Execute each tool call
                    for tc in calls_list:
                        fn_name = tc["function"]["name"]
                        raw_args_str = tc["function"]["arguments"]
                        try:
                            fn_args = json.loads(raw_args_str) if raw_args_str else {}
                        except Exception:
                            fn_args = {}

                        is_allowed, reason, req = check_tool_permission(fn_name, fn_args)

                        if req == "denied":
                            tool_result = f"Error: Tool execution denied by configuration scope. ({reason})"
                            if fn_name != "internet_search":
                                await self._add_system_message(f"[red]Tool '{fn_name}' denied: {reason}[/red]")
                        elif req == "needs_confirmation":
                            # Ask user for permission
                            confirmed, custom_reply = await self._ask_user_permission(fn_name, fn_args)
                            if confirmed:
                                tool_result = execute_tool(fn_name, fn_args)
                                if fn_name == "internet_search":
                                    search_count = 0
                                    match = re.search(r"\((\d+)\s+pages found\)", tool_result)
                                    if match:
                                        search_count = int(match.group(1))
                                    else:
                                        search_count = len(re.findall(r"\[\d+\]", tool_result))
                                    query_str = fn_args.get("query", "")
                                    await self._add_search_indicator(search_count, query_str)
                                else:
                                    await self._add_tool_message(f"{fn_name}", tool_result[:400] + ("..." if len(tool_result) > 400 else ""))
                            else:
                                if custom_reply:
                                    tool_result = f"User responded instead of approving tool: {custom_reply}"
                                    await self._add_system_message(f"Tool '{fn_name}' skipped. User responded: '{custom_reply}'")
                                else:
                                    tool_result = "Tool execution rejected by user."
                                    await self._add_system_message(f"Tool '{fn_name}' was rejected by user.")
                        else:
                            # Auto-approved
                            tool_result = execute_tool(fn_name, fn_args)
                            if fn_name == "internet_search":
                                # Count search result pages returned (parsed from actual tool result output)
                                search_count = 0
                                match = re.search(r"\((\d+)\s+pages found\)", tool_result)
                                if match:
                                    search_count = int(match.group(1))
                                else:
                                    search_count = len(re.findall(r"\[\d+\]", tool_result))
                                query_str = fn_args.get("query", "")
                                await self._add_search_indicator(search_count, query_str)
                            else:
                                await self._add_tool_message(f"{fn_name}", tool_result[:400] + ("..." if len(tool_result) > 400 else ""))

                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "content": str(tool_result),
                        })

                    # Create a new thinking container for the follow-up generation
                    asst_header = Static(f"● {self.assistant_name}", classes="message-header")
                    if self.assistant_color:
                        asst_header.styles.color = self.assistant_color
                    thinking_container = Container(
                        asst_header,
                        SpinnerThinking(color=self.assistant_color, label_text="Synthesizing…"),
                        id="thinking-row",
                        classes="message-row role-assistant",
                    )
                    await chat_view.mount(thinking_container)
                    chat_view.scroll_end(animate=False)
                    # Loop back to get model's interpretation of tool results
                    continue

                # Normal text completion (no tool calls)
                if first_token:
                    await thinking_container.remove()
                    current_assistant_msg = ChatMessage(
                        self.assistant_name,
                        "(empty response)",
                        "assistant",
                        sender_color=self.assistant_color,
                    )
                    await chat_view.mount(current_assistant_msg)

                # Keep stored conversation history CLEAN for subagents/helpers:
                # strip the router attribution imprint so the suggestions generator
                # never sees "Response returned by ..." text and can never surface
                # it as a suggestion option. The full reply (with badge) stays on screen.
                from phantombox.subagents import clean_message_for_subagent
                self.messages.append({"role": "assistant", "content": clean_message_for_subagent(full_reply)})
                self._is_generating = False
                self._current_thinking_container = None
                self._update_status_model()

                # If in phantombox/subagents mode or session subagent active, ensure session_subagent is active
                if self.model_name in ("phantombox/subagents", "subagents") and not self.session_subagent:
                    self.session_subagent = "subagent1"

                # If a subagent is active in this session, trigger automated helper scripts (e.g. suggestions)
                if self.session_subagent and not self._stop_requested:
                    from phantombox.subagents import get_subagent, run_subagent_task
                    agent_info = get_subagent(self.session_subagent)
                    if agent_info and "suggestions" in agent_info.get("scripts", []):
                        try:
                            sugg_prompt = (
                                "Based on the assistant's previous answer and conversation context, "
                                "provide 2 or 3 quick, natural follow-up response suggestions that the user might want to ask next. "
                                "Output ONLY the suggestions, one per line, starting with a bullet."
                            )
                            raw_suggestions = await run_subagent_task(
                                self.session_subagent,
                                sugg_prompt,
                                context_messages=self.messages,
                                local_port=self.port,
                            )

                            # Check if the subagent response is an error or invalid
                            lower_raw = (raw_suggestions or "").lower().strip()
                            is_error = (
                                not raw_suggestions
                                or "error" in lower_raw
                                or "usage limit" in lower_raw
                                or "quota" in lower_raw
                                or "rate limit" in lower_raw
                                or "unauthorized" in lower_raw
                                or "401" in lower_raw
                                or "429" in lower_raw
                                or "exception" in lower_raw
                            )

                            if is_error:
                                # Do NOT display the raw error or broken suggestions widget.
                                # Instead, display a clean system notice guiding the user how to disable or fix it.
                                await self._add_system_message(
                                    f"[dim yellow]It appears your subagent [bold]{self.session_subagent}[/bold] is returning an invalid response, "
                                    f"you can turn it off using [cyan]/subagents auto off[/cyan][/dim yellow]"
                                )
                            else:
                                from phantombox.subagents import sanitize_suggestion_line
                                valid_items = []
                                for line in raw_suggestions.splitlines():
                                    cleaned_item = sanitize_suggestion_line(line)
                                    if cleaned_item and len(cleaned_item) > 2:
                                        valid_items.append(cleaned_item)
                                valid_items = valid_items[:4]
                                if valid_items:
                                    sugg_widget = SuggestionsRow(self.session_subagent, valid_items)
                                    await chat_view.mount(sugg_widget)
                                    chat_view.scroll_end(animate=False)
                        except Exception:
                            await self._add_system_message(
                                f"[dim yellow]It appears your subagent [bold]{self.session_subagent}[/bold] is returning an invalid response, "
                                f"you can turn it off using [cyan]/subagents auto off[/cyan][/dim yellow]"
                            )

                break

            except Exception as e:
                self._is_generating = False
                self._current_thinking_container = None
                try:
                    await thinking_container.remove()
                except Exception:
                    pass
                if not self._stop_requested:
                    await self._add_system_message(f"[red]Error communicating with model: {e}[/red]")
                break

        self._is_generating = False
        self._current_thinking_container = None
        self._update_status_model()

    async def _ask_user_permission(self, fn_name: str, fn_args: Dict[str, Any]) -> tuple[bool, str]:
        """Prompt user in chat log and await yes/y/no/n or a custom reply."""
        args_repr = json.dumps(fn_args, indent=2)
        confirm_text = (
            f"The assistant wants to execute tool [bold {self.theme_color}]{fn_name}[/bold {self.theme_color}] in phantombox-dir with arguments:\n"
            f"[dim]{args_repr}[/dim]\n"
            f"Allow execution? Type [bold green]y[/bold green] / [bold green]yes[/bold green] to allow, "
            f"[bold red]n[/bold red] / [bold red]no[/bold red] to reject, or type a custom command."
        )
        await self._add_system_message(confirm_text)

        # Set up a future and wait for the user to submit an input
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_confirmation = future

        user_input_str = await future
        clean_input = user_input_str.strip().lower()

        if clean_input in ("y", "yes"):
            return True, ""
        elif clean_input in ("n", "no"):
            return False, ""
        else:
            # Custom text entered by user
            return False, user_input_str


def run_chat(model_name: Optional[str] = None) -> None:
    running = list_running()

    if model_name:
        p_info, _ = parse_model_identifier(model_name)
        if p_info:
            # Remote cloud model
            app = PhantomChat(model_name, 8080)
            app.run()
            return

        if model_name in running:
            port = running[model_name]["port"]
            app = PhantomChat(model_name, port)
            app.run()
            return

        # Attempt to auto-start local model
        from phantombox.runtime import start_model
        used_ports = [info.get("port", 8080) for info in running.values()]
        new_port = 8080
        while new_port in used_ports:
            new_port += 1
        success = start_model(model_name, port=new_port)
        if success:
            running = list_running()
            if model_name in running:
                app = PhantomChat(model_name, running[model_name]["port"])
                app.run()
                return

        # If not startable, open chat with model_name anyway or prompt
        app = PhantomChat(model_name, 8080)
        app.run()
        return

    # No model specified -> pick any running local model, or default to '(no model selected)'
    if running:
        default_model = next(iter(running.keys()))
        port = running[default_model]["port"]
        app = PhantomChat(default_model, port)
        app.run()
    else:
        app = PhantomChat("(no model selected)", 8080)
        app.run()
