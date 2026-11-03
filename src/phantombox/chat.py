"""Modern chat UI using Textual – OpenCode terminal style."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
from typing import Any, Dict, List, Optional

import httpx
from rich.markdown import Markdown
from textual import events, work
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Container, VerticalScroll, Horizontal, Grid
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

from phantombox import branding

# Starter prompts shown as rounded, differently-coloured chips on the welcome
# screen (replaces the old one-line help text). (label, prompt, colour)
WELCOME_CHIPS = [
    ("Tour code", "Give me a tour of this project: what it does, how it is structured, and where the entry points are.", "#58a6ff"),
    ("Script it", "Write a Python script that organises files in my workspace by type, and save it to disk.", "#3fb950"),
    ("Fix a bug", "Help me debug an issue. Ask me for the error message and the relevant file, then propose a fix.", "#f97316"),
    ("Summarise", "Read the file I name and give me a short, structured summary of what it does.", "#a371f7"),
    ("Web search", "Search the web for the latest on a topic I give you and summarise the key points with sources.", "#eab308"),
    ("Models", "Compare the models I have connected for coding, speed and cost, and recommend one.", "#2dd4bf"),
    ("Email", "Draft a short, professional email for the situation I describe.", "#ec4899"),
    ("Plan", "Help me break a project down into milestones and concrete next steps.", "#f43f5e"),
    ("Review", "Review the code I paste for bugs, clarity and performance, and suggest specific changes.", "#8b949e"),
    ("Setup", "Show me how to connect a provider with /providers --connect and verify my routes with /phantombox --routes.", "#d29922"),
]


class PromptArea(TextArea):
    """Prompt input with Enter=send, Shift+Enter=newline and ↑/↓ history.

    Behaviour (see also the prompt-box documentation in README / chat docstring):

    * ``Enter``            submits and clears the box.
    * ``Shift+Enter``      inserts a newline.
    * ``Up`` / ``Down``    step through previously sent messages. History is only
      engaged when the cursor is on the *first* line (Up) or the *last* line
      (Down), so normal multi-line cursor movement still works inside a long
      draft — this is how modern chat UIs behave.
    * The widget grows with its content (see ``on_text_area_changed``) up to a
      configurable maximum, then scrolls internally instead of pushing the
      layout off-screen.

    The owning app calls ``set_history()`` whenever a message is sent.
    """

    # Height budget: content rows plus 1 blank padding row. Both boxes start at
    # MIN_LINES (5) so the welcome and docked cards keep matching heights.
    MIN_LINES = 4
    MAX_LINES = 12

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, placeholder: str = "", **kwargs) -> None:
        super().__init__(placeholder=placeholder, **kwargs)
        self.show_line_numbers = False
        self._history: List[str] = []
        self._history_index: Optional[int] = None   # None = live draft
        self._draft: str = ""                        # draft saved when browsing

    # -- history ---------------------------------------------------------
    def set_history(self, history: List[str]) -> None:
        self._history = list(history)
        self._history_index = None

    def _restore_draft(self) -> None:
        self.load_text(self._draft)
        self.move_cursor(self.document.end)

    def _browse_history(self, delta: int) -> None:
        """Move through history by ``delta`` (-1 = older, +1 = newer)."""
        if not self._history:
            return
        if self._history_index is None:
            if delta > 0:
                return  # already at the newest entry / live draft
            self._draft = self.text
            self._history_index = len(self._history) - 1
        else:
            self._history_index += delta

        if self._history_index >= len(self._history):
            self._history_index = None
            self._restore_draft()
            return
        if self._history_index < 0:
            self._history_index = 0

        self.load_text(self._history[self._history_index])
        self.move_cursor(self.document.end)

    # -- key handling ----------------------------------------------------
    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            val = self.text.strip()
            self.load_text("")
            self.styles.height = self._height_for_lines(1)
            if val:
                self.post_message(self.Submitted(val))
            return
        if event.key == "shift+enter":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return

        if event.key == "up" and self._history:
            row = self.selection.start[0]
            if row == 0:
                event.prevent_default()
                event.stop()
                self._browse_history(-1)
                return
        if event.key == "down" and (self._history_index is not None):
            row = self.selection.start[0]
            if row >= self.document.line_count - 1:
                event.prevent_default()
                event.stop()
                self._browse_history(+1)
                return

        await super()._on_key(event)

    # -- flexing ---------------------------------------------------------
    def _height_for_lines(self, line_count: int) -> int:
        """Outer widget height for a given number of content lines.

        One extra row of breathing room keeps the text off the card's border,
        matching the reference prompt box.
        """
        visible = max(self.MIN_LINES, min(line_count, self.MAX_LINES))
        return visible + 1

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # Counting *visual* rows (wrapped lines) rather than hard newlines means
        # a long single-line message grows the box too, as modern chat UIs do.
        width = max(10, self.size.width - 2)
        visual_rows = 0
        for line in event.text_area.text.split("\n"):
            visual_rows += max(1, -(-len(line) // width))
        self.styles.height = self._height_for_lines(visual_rows)


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

    def set_label(self, text: str) -> None:
        """Change the text next to the spinner without restarting it."""
        self.label_text = text
        self.update(f"{self.FRAMES[self._frame_idx]} {text}")

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
                yield Static("No local models or connected cloud providers found.\nRun 'phantombox pull' or '/providers --connect' to connect providers.", id="picker-empty")
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


class SuggestionChip(Static):
    """Rounded, coloured starter-prompt chip shown on the welcome screen.

    Clicking one (or tabbing to it and pressing Enter) submits its prompt, which
    is the behaviour users expect from modern chat landing pages.
    """

    can_focus = True

    class Selected(Message):
        def __init__(self, prompt: str) -> None:
            super().__init__()
            self.prompt = prompt

    def __init__(self, label: str, prompt: str, color: str, **kwargs) -> None:
        # Short, single-line display label. The chip is deliberately narrow, so the
        # full text lives in the tooltip and in `prompt` — showing 30+ chars here
        # made every card truncate to a fragment ("Explain this", "Write a").
        super().__init__(label, **kwargs)
        self.prompt = prompt
        self.chip_color = color
        self.tooltip = prompt
        self.styles.border = ("round", color)
        self.styles.color = color

    def on_click(self) -> None:
        self.post_message(self.Selected(self.prompt))

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            self.post_message(self.Selected(self.prompt))

    def on_focus(self) -> None:
        self.styles.text_style = "bold"

    def on_blur(self) -> None:
        self.styles.text_style = "none"


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


class HeroLogo(Static):
    """Small, filled, shadowed "phantombox.ai" wordmark for the welcome screen.

    A single static render -- no animation, no timer. Re-coloured via
    ``set_color`` when the user runs /color.
    """

    def __init__(self, color: str = "#f97316", **kwargs) -> None:
        kwargs.setdefault("markup", False)
        rows = branding.logo_frames()[0]
        super().__init__("\n".join(rows), **kwargs)
        self.logo_color = color

    def on_mount(self) -> None:
        self.styles.color = self.logo_color

    def set_color(self, color: str) -> None:
        self.logo_color = color
        try:
            self.styles.color = color
        except Exception:
            pass

    def show_animation(self) -> None:
        """No-op: the wordmark is static now (kept for _apply_hero)."""
        pass

    def show_plain(self) -> None:
        """Fallback for terminals without block glyphs."""
        self.update("\n".join(branding.logo_plain_rows()))

    def stop(self) -> None:
        """No-op: nothing animates."""
        pass


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

    # Hero row: the animated wordmark gets room above it (a widget that starts
    # exactly at y=0 loses its top line to the container edge).
    #hero-row {
        width: 100%;
        height: auto;
        align: center middle;
        margin-bottom: 1;
        padding-top: 2;
    }

    # Hero: the animated wordmark (logo.txt) sits centred on its own row.
    # Hero row: the animated wordmark gets room above it (a widget that starts
    # exactly at y=0 loses its top line to the container edge) and the grid below
    # is sized so the whole block stays vertically centred.
    #hero-row {
        width: 100%;
        height: auto;
        align: center middle;
        margin-bottom: 1;
        padding-top: 2;
    }

    #hero-banner {
        width: auto;
        height: auto;
        content-align: center middle;
        text-align: center;
        color: #f97316;
        padding: 0 1;
    }

    /* ---------------------------------------------------------------------
       Prompt card (both welcome + dock variants)

       Requirements this implements:
         * the card background stays dark; the *border* carries the /color
           accent, together with the selected-model tag inside the footer
         * taller box -> PromptArea starts at 4 rows and flexes upward
         * the *same* look is kept once chat starts (dock card mirrors these)
       --------------------------------------------------------------------- */
    #welcome-prompt-card, #dock-prompt-card {
       width: 100%;
       height: auto;
       background: #0d1117;
       border: round #f97316;
       padding: 0;
    }

    #welcome-prompt-card:focus-within, #dock-prompt-card:focus-within {
       background: #0d1117;
       border: round #f97316;
    }

    /* Inner input layer: 1 cell inside the border, same dark colour, so the
       border reads as a ring around the dark box. */
    #welcome-prompt-card PromptArea, #dock-prompt-card PromptArea {
       width: 100%;
       /* Taller box: 5 content rows (was 3 originally, 4 in the last pass).
           The widget grows from here as the user types (see
           PromptArea._height_for_lines). */
       height: 5;
       min-height: 5;
       background: #0d1117;
       border: none;
       margin: 0 1 1 1;
       color: #e6edf3;
       /* No padding: the widget background is the one and only text layer. */
        padding: 0;
        /* Hide scrollbars: their track drew a darker stripe down the box, which
           reads as the "shadow behind the text" the redesign removes. */
        scrollbar-size: 0 0;
        scrollbar-color: #0d1117;
    }

    #welcome-prompt-card PromptArea > .text-area--cursor-line,
    #dock-prompt-card PromptArea > .text-area--cursor-line {
        background: transparent;
    }

    /* No focus highlight ring inside the text area: the card border already
       communicates focus, and an inner band reads as an unwanted shadow. */
    #welcome-prompt-card PromptArea:focus, #dock-prompt-card PromptArea:focus {
        background: transparent;
        border: none;
    }

    #welcome-box-footer {
        width: 100%;
        height: 1;
        background: #0d1117;
        padding: 0 1;
    }

    #dock-box-footer {
        width: 100%;
        height: 1;
        background: #0d1117;
        padding: 0 1;
    }

    /* Footer baseline sits inside the card; give it a little breathing room
       above so the border and the footer text don't touch. */
    #welcome-box-footer, #dock-box-footer {
        margin-top: 0;
    }

    /* ---------------------------------------------------------------------
       Starter-prompt chips (replace the old help text line)
       --------------------------------------------------------------------- */
    # Starter prompts as an evenly-sized grid of compact pills. Equal 1fr
    # columns keep every chip the same width; short labels get padding rather
    # than truncation so nothing is cut off.
    #welcome-chips {
        width: 100%;
        height: auto;
        /* Every track the same explicit width: '1fr' divided the leftover
           space unevenly (rounding), which made two chips a cell wider. */
        grid-size: 5 2;
        grid-gutter: 0 0;
        grid-rows: 3;
        margin: 0;
        align: center middle;
    }

    .chip {
        /* Fixed width in every track: 'auto'/1fr resolved to 11 or 12 cells
           depending on label length, so the pills were visibly unequal. */
        width: 16;
        min-width: 16;
        max-width: 16;
        height: 3;
        max-height: 3;
        content-align: center middle;
        text-align: center;
        background: #0d1117;
        border: round #30363d;
        padding: 0 1;
        margin: 0;
    }

    .chip:focus {
        background: #1c2430;
        text-style: bold;
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

    /* Kept for backwards compatibility: no welcome screen uses this any more */

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

    /* Docked prompt box at bottom during chat — mirrors the welcome card */
    #dock-container {
        dock: bottom;
        display: none;
        background: #0d1117;
        padding: 0 2 1 2;
        height: auto;
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
        # Set when the active model cannot use tools, so the user is told once.
        self._tools_disabled_notice_pending = False

        # Remote session state: set when this client connects to another
        # PhantomBox host (/server scan -> Sign In). While active, all host
        # settings/commands are read-only — users can only switch models,
        # interact with them, and search routes. Cleared on sign-out.
        self.remote_session: Optional[Dict[str, Any]] = None  # {host, port, username, url, token}

        # Generation control & stop handling
        self._is_generating: bool = False
        self._stop_requested: bool = False
        self._current_thinking_container: Optional[Container] = None

        # Most recent sent messages, shared by both prompt boxes for ↑/↓ recall.
        self._prompt_history: List[str] = []

        # Router attribution for the current turn ({model, provider, time, ...}).
        self._last_router_meta: Dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        with Container(id="welcome-container"):
            with Container(id="welcome-wrapper"):
                # Hero: the animated wordmark from logo.txt (no separate icon
                # columns or static brand banner any more).
                with Container(id="hero-row"):
                    yield HeroLogo(color=self.theme_color, id="hero-banner")
                # Prompt card: the colour layer reaches the card edges (no inner
                # shadow, no padding behind the text) and the box is taller.
                with Container(id="welcome-prompt-card"):
                    yield PromptArea(
                        placeholder="Ask phantombox anything, or type /help for commands...",
                        id="welcome-prompt",
                    )
                    with Horizontal(id="welcome-box-footer"):
                        yield Label(f"● {format_model_display_name(self.model_name)}", id="welcome-model-tag", classes="box-model-tag")
                        yield Label("", id="welcome-context-tag", classes="box-context-tag")
                        yield Static(classes="box-footer-spacer")
                        yield Label("enter send · shift+enter newline · ↑↓ history", classes="box-hint-tag")
                # Starter prompts replace the old help line (the footer keeps /help).
                with Grid(id="welcome-chips"):
                    for label, prompt, color in WELCOME_CHIPS:
                        yield SuggestionChip(label, prompt, color, classes="chip")

        yield VerticalScroll(id="chat-view")

        with Container(id="dock-container"):
            with Container(id="dock-prompt-card"):
                yield PromptArea(
                    placeholder="Ask phantombox anything, or type /help for commands...",
                    id="dock-prompt",
                )
                with Horizontal(id="dock-box-footer"):
                    yield Label(f"● {format_model_display_name(self.model_name)}", id="dock-model-tag", classes="box-model-tag")
                    yield Label("", id="dock-context-tag", classes="box-context-tag")
                    yield Static(classes="box-footer-spacer")
                    yield Label("enter send · shift+enter newline · ↑↓ history", classes="box-hint-tag")

    def on_mount(self) -> None:
        self._apply_border_color(self.theme_color)
        self._update_status_model()
        self._apply_hero()
        self.query_one("#welcome-prompt", PromptArea).focus()
        # Hot-load cloud provider model catalogs right when the chat opens so
        # /models shows the full list immediately. Previously the catalogs were
        # only fetched after re-saving an API key in /providers --connect, so a fresh
        # chat session showed a tiny models list until keys were re-entered.
        self._warm_provider_model_cache()
        # Connect configured MCP tool servers in the background so their tools are
        # merged into the first request without delaying the UI.
        self._connect_mcp_servers()

    def _warm_provider_model_cache(self) -> None:
        """Pre-fetch model catalogs from every connected provider at chat startup.

        Fills the in-memory model cache so /models lists full catalogs immediately,
        without requiring the user to re-save API keys via /providers --connect.
        """
        try:
            for pid in list_connected_providers():
                if pid == "phantombox":
                    continue
                asyncio.create_task(fetch_provider_models(pid))
        except Exception:
            # Never block chat startup over catalog warm-up failures
            pass

    def _connect_mcp_servers(self) -> None:
        """Connect every configured MCP server exactly once, off the UI thread.

        Each connection spawns a process / opens a socket, so it must never run on
        the Textual event loop. The manager skips already-connected servers, so
        calling this repeatedly (e.g. re-entering the chat view) is cheap. Failures
        are silent here (surfaced by /mcp list).
        """
        try:
            from phantombox.mcp import list_mcp_servers, get_mcp_manager

            if not list_mcp_servers():
                return
            mgr = get_mcp_manager()
            if getattr(mgr, "_connect_thread", None) is not None and mgr._connect_thread.is_alive():
                return  # a warm-up pass is already running
            def _warm():
                try:
                    mgr.connect_all()
                except Exception:
                    pass
                finally:
                    mgr._connect_thread = None
            t = threading.Thread(target=_warm, name="phantombox-mcp-connect", daemon=True)
            mgr._connect_thread = t
            t.start()
        except Exception:
            pass

    def _apply_hero(self) -> None:
        """Keep the animated wordmark legible on the current terminal width.

        The logo's own animation is handled by HeroLogo. This only decides whether
        the full block art fits: terminals too narrow for it get the plain
        text fallback instead of a clipped / wrapped mark.
        """
        try:
            width = self.size.width
        except Exception:
            width = 0
        if not width:
            try:
                width = self.app.size.width
            except Exception:
                width = 0
        if not width:
            import shutil as _shutil
            width = _shutil.get_terminal_size((100, 30)).columns

        try:
            hero = self.query_one("#hero-banner", HeroLogo)
        except Exception:
            return
        if width >= branding.LOGO_MIN_WIDTH and branding.supports_blocks_lenient():
            hero.show_animation()
        else:
            hero.show_plain()

    def on_resize(self, event: events.Resize) -> None:
        """Re-check whether the animated logo fits when the terminal is resized."""
        if self.in_welcome_mode:
            self._apply_hero()

    async def on_suggestion_chip_selected(self, event: SuggestionChip.Selected) -> None:
        """A starter chip was clicked or activated: send its prompt."""
        prompt = (event.prompt or "").strip()
        if not prompt:
            return
        self._switch_to_chat()
        self._record_history(prompt)
        await self._send_user_message(prompt)

    def _record_history(self, text: str) -> None:
        """Push a sent message onto both prompt boxes' ↑/↓ history."""
        entry = (text or "").strip()
        if not entry:
            return
        history = getattr(self, "_prompt_history", None)
        if history is None:
            history = self._prompt_history = []
        if not history or history[-1] != entry:
            history.append(entry)
        del history[:-100]  # keep the most recent 100 messages
        for selector in ("#welcome-prompt", "#dock-prompt"):
            try:
                self.query_one(selector, PromptArea).set_history(history)
            except Exception:
                pass

    def _apply_border_color(self, hex_or_name: str) -> None:
        """Paint the prompt-box *border* and the selected-model tag.

        The inside of the box stays dark; /color <c> only swaps the accent that
        outlines it (and tints the model tag in the footer and the hero logo).
        """
        self.theme_color = hex_or_name
        for card_id in ("#welcome-prompt-card", "#dock-prompt-card"):
            try:
                card = self.query_one(card_id)
                card.styles.border = ("round", hex_or_name)
            except Exception:
                pass

        for footer_id in ("#welcome-box-footer", "#dock-box-footer"):
            try:
                self.query_one(footer_id).styles.background = "#0d1117"
            except Exception:
                pass

        for tag_id in ("#welcome-model-tag", "#dock-model-tag"):
            try:
                tag = self.query_one(tag_id, Label)
                tag.styles.color = hex_or_name
            except Exception:
                pass

        try:
            self.query_one("#hero-banner", HeroLogo).set_color(hex_or_name)
        except Exception:
            pass

    def _switch_to_chat(self) -> None:
        """Switch view from centered welcome banner to the active chat log.

        Any text typed into the welcome prompt is carried into the docked prompt,
        so the box the user is looking at keeps its content and its size (the
        docked card intentionally mirrors the welcome card's styling).
        """
        if self.in_welcome_mode:
            welcome_prompt = self.query_one("#welcome-prompt", PromptArea)
            dock_prompt = self.query_one("#dock-prompt", PromptArea)
            draft = welcome_prompt.text

            self.query_one("#welcome-container").styles.display = "none"
            self.query_one("#chat-view").styles.display = "block"
            self.query_one("#dock-container").styles.display = "block"
            self.in_welcome_mode = False

            # The hero is gone; stop its frame timer instead of animating
            # a hidden widget for the rest of the session.
            try:
                self.query_one("#hero-banner", HeroLogo).stop()
            except Exception:
                pass

            dock_prompt.set_history(self._prompt_history)
            # Match sizes before moving focus so the box never visibly jumps: the
            # docked card is the same height as the welcome card the user just
            # typed in (see PromptArea.MIN_LINES).
            dock_prompt.styles.height = welcome_prompt.styles.height
            dock_prompt.load_text(draft)
            dock_prompt.move_cursor(dock_prompt.document.end)
            dock_prompt.focus()

    def _get_router_mode_suffix(self) -> str:
        """Effective routing mode suffix for the model tag, e.g. '/shuffle'.

        Priority matches the routing engine: overdrive > provider > shuffle.
        Exclusions are passive filtering and don't change the suffix, but are
        surfaced in /phantombox --routes * --status output.
        Returns '' when no special mode is engaged (plain adaptive routing).
        """
        if self.model_name not in ("phantombox/router", "router", "phantombox/subagents", "subagents"):
            return ""
        try:
            from phantombox.router import get_overdrive_model, get_provider_filter, is_shuffle_mode
            od = get_overdrive_model()
            if od:
                return "/overdrive"
            pf = get_provider_filter()
            if pf:
                return f"/{pf}"
            if is_shuffle_mode():
                return "/shuffle"
        except Exception:
            pass
        return ""

    def _tools_for_active_model(self) -> List[Dict[str, Any]]:
        """Tool schemas to send this turn, scoped to the active model's provider.

        The rule is: **built-in PhantomBox tools + the tools reachable from this
        model's provider**, and nothing else.

        * ``phantombox/router`` -> the full pool, because the router re-pools per
          route before dispatch (see stream_router_completion).
        * a cloud model -> PhantomBox built-ins. Cloud APIs do not publish a tool
          catalog of their own; what a hosted model can call is decided by the
          ``tools`` array the client sends, so extra capabilities are exposed by
          attaching MCP servers (/mcp add, /mcp add-http) rather than by having a
          key. Tools from another provider's MCP servers never leak through.
        * a model known to reject tools -> empty list, with a notice.
        """
        from phantombox.capabilities import supports_tools
        from phantombox.providers import _model_tool_source, _tools_for_source, parse_model_identifier

        self._tools_disabled_notice_pending = False
        try:
            p_info, raw_model = parse_model_identifier(self.model_name)
            source = _model_tool_source(self.model_name)
            if p_info and p_info.get("api_type") == "router":
                return get_tool_definitions()
            scoped = _tools_for_source(source, get_tool_definitions(), raw_model or self.model_name)
            if not scoped:
                self._tools_disabled_notice_pending = True
            return scoped
        except Exception:
            pass
        return get_tool_definitions()

    def _tool_prep_label(self) -> str:
        """Human-readable status for the tool-preparation phase of a turn.

        Shows WHAT is happening (cache read vs. MCP warm-up in progress) so a
        slow first message never looks like a frozen app.
        """
        try:
            from phantombox.mcp import get_mcp_manager
            mgr = get_mcp_manager()
            servers = mgr.servers_config()
            if not servers:
                return "Preparing tools…"
            ready = [n for n, c in mgr.connections.items() if c.status == "ready"]
            ct = getattr(mgr, "_connect_thread", None)
            connecting = [n for n in servers if n not in ready]
            if ct is not None and ct.is_alive():
                return f"Connecting MCP servers… ({len(ready)}/{len(servers)} ready)"
            n_tools = len(mgr.tool_definitions())
            if connecting and (not ready or not n_tools):
                return f"Loading MCP servers… ({len(ready)}/{len(servers)} ready)"
            if connecting:
                return f"Preparing tools… ({len(ready)}/{len(servers)} MCP servers ready, {n_tools} tool(s) cached)"
            return f"Preparing tools… ({n_tools} MCP tool(s) cached)"
        except Exception:
            return "Preparing tools…"

    def _update_status_model(self) -> None:
        display_name = format_model_display_name(self.model_name) + self._get_router_mode_suffix()
        # Remote sessions show the host address right before the model name,
        # so the user always knows which network they are chatting on.
        sess = self.remote_session or None
        if sess:
            display_name = f"[cyan]@ {sess.get('host')}:{sess.get('port')}[/cyan] {display_name}"
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

    def _is_remote_session(self) -> bool:
        """True when this client is signed into a remote PhantomBox host.

        While active, everything host-side is read-only: users can only switch
        models, interact with them and search routes. Settings, MCP servers,
        provider keys, server user management etc. are blocked.
        """
        return bool(self.remote_session)

    def _remote_block_message(self) -> str:
        sess = self.remote_session or {}
        return (
            f"[bold red]Read-only mode:[/bold red] You are signed into host [cyan]{sess.get('host')}:{sess.get('port')}[/cyan]. "
            "Remote connections cannot change host settings, add MCP servers or manage users. "
            "You can only switch models, chat with them and search routes. "
            "Sign out with [cyan]/server sign-out[/cyan] to unlock this device."
        )

    def _remote_blocks_command(self, cmd_line: str) -> bool:
        """Check if this slash command is blocked during a remote session.

        '/phantombox' route-search stays open; '/server' is allowed for the
        sign-out + status + scan paths only (the handler gates the rest).
        """
        if not self._is_remote_session():
            return False
        parts = cmd_line.split()
        if not parts:
            return False
        cmd = parts[0].lower()
        if cmd == "/phantombox":
            return False  # route search is open to remote users
        if cmd == "/server":
            subcmd = parts[1].lower() if len(parts) > 1 else ""
            return subcmd not in ("sign-out", "signout", "status", "scan", "--discover", "discover", "")
        if cmd in ("/mcp", "/providers", "/subagents", "/color", "/runtime", "/setup", "/settings"):
            return True
        return False

    async def _add_system_message(self, text: str) -> None:
        chat_view = self.query_one("#chat-view")
        msg = ChatMessage("System", text, "system")
        await chat_view.mount(msg)
        chat_view.scroll_end(animate=False)

    async def _start_status(self, label: str, color: str = "#38bdf8") -> "SpinnerThinking | None":
        """Mount a labelled spinner and return it (None if the view is unavailable).

        Callers must remove it in a finally block; a spinner left behind keeps
        animating and leaves the user stuck on a long operation.
        """
        try:
            chat_view = self.query_one("#chat-view", VerticalScroll)
            spinner = SpinnerThinking(color=color, label_text=label)
            await chat_view.mount(spinner)
            chat_view.scroll_end(animate=False)
            return spinner
        except Exception:
            return None

    @staticmethod
    async def _stop_status(spinner) -> None:
        if spinner is None:
            return
        try:
            await spinner.remove()
        except Exception:
            pass

    async def _confirm_yes_no(self, question: str) -> bool:
        """Ask a y/n question in the chat log and wait for the answer.

        The typed answer is consumed here (see on_prompt_submitted's
        _pending_confirmation branch), so it never reaches the model.
        """
        await self._add_system_message(question)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_confirmation = future
        try:
            answer = (await future).strip().lower()
        finally:
            self._pending_confirmation = None
        return answer in ("y", "yes")

    async def _ask_input(self, prompt: str, default: str = "") -> str:
        """Ask the user for free text in the chat log and return the typed value.

        Uses the same pending-confirmation channel as _confirm_yes_no, so the
        answer is consumed by the interactive flow and never reaches the model.
        Returns the empty string when the user types nothing (or presses enter on
        an empty box with no default).
        """
        suffix = f" [dim](default: {default})[/dim]" if default else ""
        await self._add_system_message(prompt + suffix)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_confirmation = future
        try:
            answer = (await future).strip()
        except asyncio.CancelledError:
            answer = ""
        finally:
            self._pending_confirmation = None
        return answer or default

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

        # Slash commands are recalled with ↑/↓ just like chat messages.
        if raw_text.startswith("/"):
            self._record_history(raw_text)

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

        # Remote-session gate: everything host-side is read-only while signed
        # into another host. Route search (/phantombox) and sign-out stay open.
        if self._remote_blocks_command(cmd_line):
            await self._add_system_message(self._remote_block_message())
            return

        # /mcp list can block on a y/n confirmation. Run it as a background task
        # so the UI, the prompt box and every other command keep working while
        # the user decides -- awaiting it here froze the whole app behind the
        # prompt ("it prints the message but nothing works after").
        if cmd == "/mcp" and len(parts) > 1 and parts[1].lower() in ("list", "--list", "ls"):
            self._switch_to_chat()
            self.run_worker(self._handle_mcp_command(parts[1:], " ".join(parts[1:])), exclusive=False)
            return
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

        if cmd in ("/providers", "/provider"):
            await self._handle_providers_command(args, raw_arg)
            return

        if cmd == "/mcp":
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
                await self._open_models_picker_async()
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
                "  • /providers --connect - Connect cloud model provider API keys (OpenRouter, Groq, Claude...)\n"
                "  • /providers list - List connected providers, key status & model counts\n"
                "  • /providers --refresh - Re-fetch model catalogs now (bypasses the 24h cache)\n"
                "  • /mcp - Connect external MCP tool servers (stdio or HTTP)\n"
                "  • /mcp list - Show MCP servers, their tools, and which models can call them\n"
                "  • /mcp --tools <provider> - Tools a provider's models can call\n"
                "  • /mcp --models <provider> - A provider's models and their tool support\n"
                "  • /user <name> - Set user name for this session\n"
                "  • /user --color <color> - Set user display name color (e.g. /user --color cyan)\n"
                "  • /user --global <name> - Save user name permanently across sessions\n"
                "  • /assistant <name> - Set assistant name for this session\n"
                "  • /assistant --color <color> - Set assistant display name color\n"
                "  • /color <color> - Change prompt border color\n"
                "  • /server - View server status, users & signup auth mode\n"
                "  • /server start [port] - Start LAN AI server on this device\n"
                "  • /server close (or /server stop) - Close and stop LAN server\n"
                "  • /server scan - Scan the network for other PhantomBox servers (shows device type)\n"
                "  • /server --connect <host:port> - Connect to another server (interactive sign-in)\n"
                "  • /server sign-out - End the remote session (returns to normal mode)\n"
                "  • /server config <open|approval|email> - Set signup auth mode for this host\n"
                "  • /server user <name> <password> - Create user (host-side, approved immediately)\n"
                "  • /server user --approvals - View & approve/deny pending signups\n"
                "  • /server users - List approved users and pending signups\n"
                "  • /server discoverable <on|off> - Set broadcast discovery for phones/laptops\n"
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
                "  • /phantombox --routes - Show every cloud model returning 200 OK\n"
                "  • /phantombox --routes rescan - Re-ping all cloud models now and rebuild verified routes\n"
                "  • /phantombox --routes rescan <provider> - Quick rescan of a single provider's routes\n"
                "  • /phantombox --routes exclude|include|shuffle|overdrive|provider - Advanced route controls\n"
                "  • /phantombox --routes --status - Show routing mode states & current prompt tag\n"
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

    async def _handle_providers_command(self, args: list[str], raw_arg: str) -> None:
        """Handle /providers — cloud LLM provider keys & model catalogs ('/mcp --connect' alias)."""
        self._switch_to_chat()
        if not args or args[0].lower() in ("--connect", "-c", "connect", "gui", "modal"):
            self._open_mcp_modal()
            return

        subcmd = args[0].lower()
        if subcmd in ("--refresh", "refresh", "reload"):
            from phantombox.providers import invalidate_catalog, fetch_provider_models, list_connected_providers as _lcp
            invalidate_catalog()
            pids = [p for p in _lcp() if p != "phantombox"]
            if not pids:
                await self._add_system_message("[yellow]No providers connected — nothing to refresh.[/yellow]")
                return
            await self._add_system_message(f"[dim]Refreshing model catalogs for {len(pids)} provider(s)…[/dim]")
            results = await asyncio.gather(*(fetch_provider_models(p, force=True) for p in pids), return_exceptions=True)
            ok = sum(1 for r in results if isinstance(r, list) and r)
            await self._add_system_message(
                f"[green]Catalog refresh complete:[/green] {ok}/{len(pids)} provider(s) returned models."
                + (" [yellow]The rest failed — check keys/network.[/yellow]" if ok < len(pids) else "")
            )
            return

        if subcmd == "list":
            connected = [p for p in list_connected_providers() if p != "phantombox"]
            if not connected:
                await self._add_system_message(
                    "No external providers connected yet.\n"
                    "Use [cyan]/providers --connect[/cyan] to connect OpenRouter, Groq, Claude, OpenAI, Gemini, NVIDIA, etc.\n\n"
                    "[dim]Cloud providers supply chat models only; the tools given to them are PhantomBox's own. "
                    "For external tool servers use [cyan]/mcp[/cyan].[/dim]"
                )
            else:
                from phantombox.providers import catalog_cache_age, CATALOG_TTL_SECONDS
                from phantombox.router import get_verified_models

                routes = get_verified_models()
                route_counts: Dict[str, int] = {}
                for v in routes:
                    rp = (v.get("provider") or "").lower()
                    route_counts[rp] = route_counts.get(rp, 0) + 1

                lines = ["[bold]Connected Providers (models only)[/bold]"]
                for pid in connected:
                    p = get_provider(pid)
                    p_name = p["name"] if p else pid
                    key = get_provider_key(pid)
                    key_state = "[green]key set[/green]" if key else "[yellow]no key[/yellow]"

                    models = fetch_cached_or_default_models(pid)
                    age = catalog_cache_age(pid)
                    if not models:
                        model_state = "[yellow]0 models (catalog unavailable)[/yellow]"
                    elif age is None:
                        model_state = f"{len(models)} model(s)"
                    else:
                        age_txt = f"{int(age // 3600)}h" if age >= 3600 else f"{int(age // 60)}m"
                        expiry = "" if age <= CATALOG_TTL_SECONDS else " [yellow](stale)[/yellow]"
                        model_state = f"{len(models)} model(s), cached {age_txt} ago{expiry}"

                    n_routes = route_counts.get(pid.lower(), 0)
                    route_state = (
                        f"[green]{n_routes} verified route(s)[/green]" if n_routes
                        else "[dim]no verified routes (run /phantombox --routes rescan)[/dim]"
                    )

                    # Tool support is a property of the *models*, so report it
                    # truthfully rather than claiming the provider supplies tools.
                    from phantombox.capabilities import supports_tools
                    capable = sum(1 for m in models if supports_tools(pid, m) is True)
                    incapable = sum(1 for m in models if supports_tools(pid, m) is False)
                    if not models:
                        tools_state = "[dim]n/a[/dim]"
                    elif incapable == 0:
                        tools_state = f"[dim]{capable}/{len(models)} models accept tools (local[/dim] [dim]tools)[/dim]"
                    else:
                        tools_state = f"[dim]{capable}/{len(models)} models accept tools, {incapable} cannot[/dim]"

                    lines.append(f"  • [green]●[/green] [bold]{p_name}[/bold] ([dim]{pid}[/dim])")
                    lines.append(f"      key: {key_state}  •  catalog: {model_state}")
                    lines.append(f"      routes: {route_state}  •  {tools_state}")

                lines.append(
                    "\n[dim]Per provider: 'models' are the live catalog from that provider; 'routes' are the models "
                    "that answered a 200 OK probe; 'accept tools' is how many of its models can call tools. "
                    "All tool execution is local to PhantomBox. "
                    "External tool servers are added with [cyan]/mcp add[/cyan].[/dim]"
                )
                lines.append("Switch models using [cyan]/models[/cyan].")
                await self._add_system_message("\n".join(lines))
            return

        if subcmd == "disconnect" and len(args) > 1:
            target_pid = args[1].lower()
            set_provider_key(target_pid, "")
            await self._add_system_message(f"Provider '[bold]{target_pid}[/bold]' disconnected.")
            return

        self._open_mcp_modal()

    def _warn_if_no_tools(self, server_name: str) -> None:
        """Nothing to say when tools are present; otherwise keep it quiet too.

        Kept as a hook so a later change can flag zero-tool servers without
        touching the add flow.
        """
        return

    def _mcp_tool_pool_rows(self) -> list[str]:
        """Per MCP server: its tools and which connected models can call them."""
        from phantombox.mcp import get_mcp_manager

        mgr = get_mcp_manager()
        capable = [(mid, label) for mid, label, can in self._models_supporting_tools() if can]
        rows: list[str] = []
        for name in mgr.connections:
            tools = mgr.get_tools(name)
            if not tools:
                continue
            rows.append(f"  • [bold]{name}[/bold] — {len(tools)} tool(s)")
            if capable:
                shown = ", ".join(label for _mid, label in capable[:4])
                more = f" (+{len(capable) - 4} more)" if len(capable) > 4 else ""
                rows.append(f"      [green]usable by {len(capable)} model(s):[/green] {shown}{more}")
            else:
                rows.append("      [yellow]no tool-capable models available[/yellow]")
        return rows

    def _model_tool_pool_rows(self) -> list[str]:
        """Per connected model: which tool sources it receives.

        This is the inverse view of _mcp_tool_pool_rows and answers "what does the
        selected model actually see when it runs a turn?".
        """
        from phantombox.mcp import get_mcp_manager
        from phantombox.tools import get_tool_definitions

        mgr = get_mcp_manager()
        n_builtin = len([t for t in get_tool_definitions() if "__" not in t["function"]["name"]])
        mcp_servers = [s for s in mgr.connections if mgr.get_tools(s)]

        rows: list[str] = []
        for model_id, label, can in self._models_supporting_tools():
            if not can:
                rows.append(f"  • {label} [red]— no function calling (built-ins unavailable)[/red]")
                continue
            sources = f"{n_builtin} built-in"
            if mcp_servers:
                sources += f" + MCP ({', '.join(mcp_servers)})"
            rows.append(f"  • {label} [green]← {sources}[/green]")
        return rows

    def _models_supporting_tools(self) -> list[tuple[str, str, bool]]:
        """Every selectable model plus whether it can call tools."""
        from phantombox.capabilities import tools_supported_or_unknown
        from phantombox.providers import parse_model_identifier

        out: list[tuple[str, str, bool]] = []
        for model_id, label, _ok in self._get_all_available_models():
            p_info, raw = parse_model_identifier(model_id)
            if p_info and p_info.get("api_type") == "router":
                can = True  # router forwards to whichever route wins
            else:
                can = tools_supported_or_unknown((p_info or {}).get("id", ""), raw or model_id)
            out.append((model_id, label, can))
        return out

    async def _handle_mcp_command(self, args: list[str], raw_arg: str) -> None:
        """Handle /mcp — external MCP tool servers and which models can use them."""
        from phantombox.mcp import list_mcp_servers

        self._switch_to_chat()
        servers = list_mcp_servers()
        subcmd = args[0].lower() if args else ""

        # ---- /mcp --tools provider --list / /mcp --tools --list ----
        # (accepts both spellings; the provider slot may be omitted)
        if subcmd in ("--tools", "tools", "--models", "models"):
            from phantombox.providers import list_connected_providers, get_provider

            target = None
            for a in args[1:]:
                if a.lower() not in ("--list", "list", "--ls", "--status", "status"):
                    target = a
                    break
            wants_list = any(a.lower() in ("--list", "list", "--ls") for a in args[1:])
            connected = [p for p in list_connected_providers() if p != "phantombox"]

            if wants_list or not target:
                lines = ["[bold cyan]Connected providers:[/bold cyan]"]
                if not connected:
                    lines.append("  [dim](none — connect one with /providers --connect)[/dim]")
                for pid in connected:
                    info = get_provider(pid) or {}
                    n_models = len(fetch_cached_or_default_models(pid))
                    lines.append(f"  • [bold]{pid}[/bold] ({info.get('name', pid)}) — {n_models} model(s)")
                lines.append(
                    "\n[dim]/mcp --tools <provider> — tools that provider's models can call\n"
                    "/mcp --models <provider> — that provider's models and tool support[/dim]"
                )
                await self._add_system_message("\n".join(lines))
                return

            from phantombox.router import _resolve_connected_provider

            resolved = _resolve_connected_provider(target)
            if not resolved:
                await self._add_system_message(
                    f"[yellow]'{target}' is not a connected provider.[/yellow] "
                    f"Connected: {', '.join(connected) if connected else 'none'}.\n"
                    "[dim]Run [cyan]/mcp --tools --list[/cyan] to see the exact names.[/dim]"
                )
                return

            # Provider resolution may live-probe the endpoint (set_provider_filter
            # does), and get_tool_definitions() connects MCP servers, so both the
            # lookup and the listing get a visible spinner instead of a frozen box.
            spinner = await self._start_status("Listing…")
            try:
                models = await asyncio.to_thread(fetch_cached_or_default_models, resolved)
                info = get_provider(resolved) or {}
            finally:
                await self._stop_status(spinner)

            if subcmd in ("--models", "models"):
                lines = [f"[bold cyan]{info.get('name', resolved)} models ({len(models)}):[/bold cyan]"]
                if not models:
                    lines.append("  [dim](no catalog — run /providers --refresh)[/dim]")
                for mid in models:
                    from phantombox.capabilities import tool_capability_label
                    cap = tool_capability_label(resolved, mid)
                    colour = "green" if cap == "tools: yes" else ("red" if cap == "tools: no" else "yellow")
                    lines.append(f"  • [bold]{mid}[/bold] [{colour}]{cap}[/{colour}]")
                lines.append(
                    "\n[dim]Models marked 'tools: no' only get PhantomBox built-in tools when they\n"
                    "support them; the rest receive built-ins + every connected MCP tool.[/dim]"
                )
                await self._add_system_message("\n".join(lines))
                return

            # ---- /mcp --tools <provider> ----
            # Shows EXACTLY what a model of this provider receives on a turn:
            # PhantomBox built-ins + the MCP tools attached to this app. Cloud
            # providers do not publish their own tool catalog, so an API key can
            # never add tools by itself -- see the note printed at the end.
            from phantombox.mcp import get_mcp_manager
            from phantombox.capabilities import tools_supported_or_unknown
            from phantombox.tools import get_tool_definitions

            mgr = get_mcp_manager()
            spinner = await self._start_status("Listing…")
            try:
                all_tools = await asyncio.to_thread(get_tool_definitions)
                capable_models = [m for m in models if tools_supported_or_unknown(resolved, m)]
            finally:
                await self._stop_status(spinner)
            lines = [
                f"[bold cyan]Tools available to {info.get('name', resolved)} models[/bold cyan]",
                f"[dim]source: PhantomBox built-ins + MCP servers attached to this app"
                f" ({len(mgr.connections)} connected)[/dim]",
            ]

            all_tools = get_tool_definitions()
            all_tools = get_tool_definitions()
            builtin = [t for t in all_tools if "__" not in t["function"]["name"]]
            lines.append(f"\n[bold]Built-in ({len(builtin)}):[/bold] " + ", ".join(
                f"[cyan]{t['function']['name']}[/cyan]" for t in builtin
            ))

            if not mgr.connections:
                lines.append("\n[bold]MCP:[/bold] [dim]none attached[/dim]")
                lines.append(
                    "\n[yellow]An API key does not add tools.[/yellow] Cloud providers only run "
                    "the tools a client sends, so to give these models extra capabilities attach "
                    "an MCP server (or a hosted/remote MCP endpoint) to PhantomBox:\n"
                    "  [cyan]/mcp add github npx -y @modelcontextprotocol/server-github[/cyan]\n"
                    "  [cyan]/mcp add-http hosted https://mcp.example.com/mcp <token>[/cyan]"
                )
            else:
                lines.append("\n[bold]MCP servers:[/bold]")
                for srv in mgr.connections:
                    tools = mgr.get_tools(srv)
                    if not tools:
                        continue
                    lines.append(f"  • [bold]{srv}[/bold] — {len(tools)} tool(s)")
                    for t in tools:
                        lines.append(f"      - [cyan]{t['qualified_name']}[/cyan] {t['description'][:70]}")

            lines.append(f"\n[dim]{len(capable_models)}/{len(models)} models can call these tools."
                         + ("" if capable_models else " This provider has no tool-capable model.")
                         + "[/dim]")
            await self._add_system_message("\n".join(lines))
            return

        # Legacy alias: /mcp with no args (or --connect) behaves as before.
        if not args or args[0].lower() in ("--connect", "-c", "connect", "gui", "modal"):
            if servers:
                await self._add_system_message(
                    f"[bold cyan]MCP servers connected ({len(servers)}):[/bold cyan]\n"
                    + "\n".join(f"  • {n}" for n in servers)
                    + "\n\n[dim]Add one with [cyan]/mcp add <name> <command...>[/cyan] or [cyan]/mcp add-http <name> <url>[/cyan]. "
                    "For cloud model API keys use [cyan]/providers --connect[/cyan].[/dim]"
                )
            else:
                await self._add_system_message(
                    "[bold cyan]MCP — external tool servers[/bold cyan]\n"
                    "No MCP servers connected yet.\n\n"
                    "Connect one:\n"
                    "  • [cyan]/mcp add <name> <command> [args...][/cyan] — stdio server (e.g. npx -y @modelcontextprotocol/server-filesystem <dir>)\n"
                    "  • [cyan]/mcp add-http <name> <url> [bearer_token][/cyan] — Streamable HTTP server\n"
                    "  • [cyan]/mcp list[/cyan] — show servers and the tools they expose\n"
                    "  • [cyan]/mcp remove <name>[/cyan] — disconnect a server\n\n"
                    "[dim]Cloud model API keys are managed separately with [cyan]/providers --connect[/cyan]."
                    " MCP tools (e.g. github__create_issue) are merged into the model's tool list automatically.[/dim]"
                )
            return

        if subcmd == "list":
            if not servers:
                await self._add_system_message(
                    "No MCP servers configured. Add one with [cyan]/mcp add <name> <command...>[/cyan]."
                )
                return
            from phantombox.mcp import get_mcp_manager
            mgr = get_mcp_manager()

            # A busy install can hold hundreds of models across providers; probing
            # every catalog up front is slow, so warn first and let the user opt
            # out. The count comes from cached catalogs (no network yet).
            from phantombox.providers import list_connected_providers

            connected = [p for p in list_connected_providers() if p != "phantombox"]
            n_models = sum(len(fetch_cached_or_default_models(p)) for p in connected)
            if n_models:
                proceed = await self._confirm_yes_no(
                    f"[bold yellow]Are you sure you want to list all models?[/bold yellow] "
                    f"[bold]{n_models}[/bold] model(s) are available across {len(connected)} provider(s) "
                    "and this might take a significant amount of time.\n"
                    "Continue? Type [bold green]y[/bold green] / [bold green]yes[/bold green] to continue, "
                    "[bold red]n[/bold red] / [bold red]no[/bold red] to cancel."
                )
                if not proceed:
                    await self._add_system_message("[dim]Cancelled.[/dim]")
                    return

            spinner = await self._start_status("Listing…")
            try:
                await asyncio.to_thread(mgr.connect_all)
            finally:
                await self._stop_status(spinner)

            lines = [f"[bold cyan]MCP Servers ({len(servers)}):[/bold cyan]"]
            for name, spec in servers.items():
                tools = mgr.get_tools(name)
                conn = mgr.connections.get(name)
                transport = spec.get("url") or " ".join([spec.get("command", "")] + list(spec.get("args", [])))

                # Report real connection state, not just tool count: a server
                # that failed to start and one that started but exposes nothing
                # are different problems.
                if conn is None:
                    state = "[yellow]○ not connected[/yellow]"
                elif conn.status == "error":
                    state = f"[red]✗ failed[/red] [dim]{conn.error[:70]}[/dim]"
                elif conn.status == "ready" and tools:
                    state = f"[green]● ready — {len(tools)} tool(s)[/green]"
                elif conn.status == "ready":
                    state = "[yellow]○ connected, exposes 0 tools[/yellow]"
                else:
                    state = f"[yellow]○ {conn.status}[/yellow]"

                lines.append(f"  • [bold]{name}[/bold] {state}")
                lines.append(f"      [dim]{transport}[/dim]")
                for t in tools:
                    lines.append(f"      - [cyan]{t['qualified_name']}[/cyan] {t.get('description', '')[:80]}")
            lines.append(
                "\n[dim]Remove with /mcp remove <name>. Tools are namespaced server__tool and always ask "
                "for confirmation unless allow-listed under mcp_auto_approve.[/dim]"
            )

            # Tools <-> models mapping: which models can actually call these tools,
            # and what each connected model receives on a turn.
            tool_rows = self._mcp_tool_pool_rows()
            if tool_rows:
                lines.append("\n[bold cyan]Tools by model access:[/bold cyan]")
                lines.extend(tool_rows)

            model_rows = self._model_tool_pool_rows()
            if model_rows:
                lines.append("\n[bold cyan]Model tool pools:[/bold cyan]")
                lines.extend(model_rows)

            lines.append(
                "\n[dim]Per provider: [cyan]/mcp --tools <provider>[/cyan] · "
                "[cyan]/mcp --models <provider>[/cyan] · "
                "[cyan]/mcp --tools provider --list[/cyan][/dim]"
            )
            await self._add_system_message("\n".join(lines))
            return

        if subcmd in ("add", "add-http", "connect") and len(args) > 1:
            from phantombox.mcp import add_mcp_server, probe_mcp_server, list_mcp_servers as _list_servers
            name = args[1].lower()
            if subcmd == "add-http":
                if len(args) < 3:
                    await self._add_system_message("Usage: [cyan]/mcp add-http <name> <url> [bearer_token][/cyan]")
                    return
                spec = {"url": args[2], "token": args[3] if len(args) > 3 else ""}
            else:
                if len(args) < 3:
                    await self._add_system_message("Usage: [cyan]/mcp add <name> <command> [args...][/cyan]")
                    return
                spec = {"command": args[2], "args": list(args[3:])}

            ok, msg = add_mcp_server(name, spec)
            if not ok:
                await self._add_system_message(f"[bold red]✗ Not added:[/bold red] {msg}")
                return

            # Saved first, then probed. The probe is what decides the final
            # message, so "saved but the server never started" can no longer be
            # reported as a plain success (that mismatch is why an error was
            # followed by the server showing up in /mcp list).
            spinner = await self._start_status("Adding MCP…")
            try:
                ok, msg = await asyncio.to_thread(probe_mcp_server, name)
            finally:
                await self._stop_status(spinner)

            if ok:
                await self._add_system_message(f"[bold green]✓ Added:[/bold green] {msg}")
                self._warn_if_no_tools(name)
            elif name in _list_servers():
                await self._add_system_message(
                    f"[bold yellow]⚠ Saved but not connected:[/bold yellow] {msg}\n"
                    f"[dim]'{name}' is stored and will retry on first use. Fix the command, then run "
                    f"[cyan]/mcp add {name} <command>[/cyan] again, or [cyan]/mcp remove {name}[/cyan] to discard it.[/dim]"
                )
            else:
                await self._add_system_message(f"[bold red]✗ Not added:[/bold red] {msg}")
            return

        if subcmd in ("remove", "rm", "disconnect") and len(args) > 1:
            from phantombox.mcp import remove_mcp_server
            ok, msg = remove_mcp_server(args[1].lower())
            style = "green" if ok else "yellow"
            await self._add_system_message(f"[{style}]{msg}[/{style}]")
            return

        await self._add_system_message(
            "[bold cyan]MCP commands[/bold cyan]\n"
            "  • [cyan]/mcp[/cyan] — overview & help\n"
            "  • [cyan]/mcp list[/cyan] — servers, their tools, and which models can call them\n"
            "  • [cyan]/mcp --tools <provider>[/cyan] — tools a provider's models can call\n"
            "  • [cyan]/mcp --tools provider --list[/cyan] — connected providers\n"
            "  • [cyan]/mcp --models <provider>[/cyan] — a provider's models and tool support\n"
            "  • [cyan]/mcp add <name> <command> [args...][/cyan] — stdio server\n"
            "  • [cyan]/mcp add-http <name> <url> [token][/cyan] — Streamable HTTP server\n"
            "  • [cyan]/mcp remove <name>[/cyan] — disconnect a server\n\n"
            "[dim]Cloud provider API keys: [cyan]/providers --connect[/cyan][/dim]"
        )

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
        """Handle /server commands: start, close, scan, connect, config, user, users, sign-out, etc."""
        self._switch_to_chat()
        from phantombox.server import (
            start_server,
            stop_server,
            is_server_running,
            set_user,
            remove_user,
            list_users,
            list_pending_users,
            approve_user,
            deny_user,
            set_auth_mode,
            set_discoverable,
            is_discoverable,
            get_local_ip,
            get_server_config,
            discover_lan_servers,
            get_cloud_info,
            save_remote_server,
            list_remote_servers,
            remove_remote_server,
            AUTH_MODE_OPEN,
            AUTH_MODE_APPROVAL,
            AUTH_MODE_EMAIL,
        )

        if not args:
            # Display current server status & info
            running = is_server_running()
            cfg = get_server_config()
            port = cfg.get("port", 8443)
            local_ip = get_local_ip()
            users = list_users()
            disc = is_discoverable()
            auth_mode = cfg.get("auth_mode", AUTH_MODE_OPEN)

            status_str = f"[bold green]ONLINE[/bold green] (listening on http://{local_ip}:{port})" if running else "[bold yellow]OFFLINE[/bold yellow]"
            disc_str = "[green]ENABLED[/green]" if disc else "[red]DISABLED[/red]"
            mode_str = {
                AUTH_MODE_OPEN: "[green]open[/green] (instant signup, credentials saved immediately)",
                AUTH_MODE_APPROVAL: "[yellow]approval[/yellow] (new signups wait for your approval)",
                AUTH_MODE_EMAIL: "[red]email[/red] (requires email verification — not supported yet)",
            }.get(auth_mode, auth_mode)

            lines = [
                "[bold cyan]PhantomBox AI Server[/bold cyan]",
                f"• Status: {status_str}",
                f"• Local IP (Hotspot/LAN): [bold]{local_ip}[/bold] (Port: {port})",
                f"• Network Discovery Beacon: {disc_str}",
                f"• Signup auth mode: {mode_str}",
                f"• Approved users ({len(users)}): [bold]{', '.join(users.keys()) or '(none)'}[/bold]",
                f"• Pending signups ({len(list_pending_users())})",
            ]
            if self._is_remote_session():
                sess = self.remote_session or {}
                lines.append(f"• [bold magenta]Remote session:[/bold magenta] signed in as {sess.get('username')} @ {sess.get('host')}:{sess.get('port')} [dim](read-only)[/dim]")

            lines += [
                "\n[bold]Server Commands:[/bold]",
                "  • [cyan]/server start [port][/cyan] - Open server for network connections",
                "  • [cyan]/server close[/cyan] - Close and shutdown server",
                "  • [cyan]/server scan[/cyan] - Scan LAN/hotspot for other PhantomBox servers (shows device type)",
                "  • [cyan]/server --connect <host:port>[/cyan] - Connect to a server (interactive sign-in)",
                "  • [cyan]/server sign-out[/cyan] - End the remote session on this device",
                "  • [cyan]/server config[/cyan] - View or change signup auth mode (open|approval|email)",
                "  • [cyan]/server user <name> <password>[/cyan] - Create/update a user (host-side)",
                "  • [cyan]/server user rm <name>[/cyan] - Remove user",
                "  • [cyan]/server user --approvals[/cyan] - View & approve/deny pending signups",
                "  • [cyan]/server users[/cyan] - List approved users",
                "  • [cyan]/server discoverable <on|off>[/cyan] - Toggle phone discovery",
                "  • [cyan]/server remotes[/cyan] - List connected remote servers",
            ]
            await self._add_system_message("\n".join(lines))
            return

        subcmd = args[0].lower()

        # 1. /server start [--public-ip X.X.X.X] [--bind Y.Y.Y.Y] [port]
        if subcmd == "start":
            port = None
            public_ip = ""
            bind_host = ""
            for i in range(1, len(args)):
                if args[i] == "--public-ip" and i + 1 < len(args):
                    public_ip = args[i + 1]
                    i += 1
                elif args[i] == "--bind" and i + 1 < len(args):
                    bind_host = args[i + 1]
                    i += 1
                elif args[i].isdigit():
                    port = int(args[i])
            success, msg = start_server(port=port, public_ip=public_ip, host=bind_host)
            if success:
                cfg = get_server_config()
                p = cfg.get("port", 8443)
                cloud = get_cloud_info()
                ip = cloud.get("public_ip") or get_local_ip()
                note = (
                    "\n[dim]VPS/cloud server: devices on this WiFi/Hotspot AND over the internet can reach this address "
                    "(firewall/security group permitting).[/dim]"
                    if cloud.get("is_cloud") else ""
                )
                await self._add_system_message(
                    f"[bold green]Server started successfully![/bold green]\n"
                    f"Address: [bold cyan]http://{ip}:{p}[/bold cyan]\n"
                    f"Devices on this WiFi/Hotspot can now connect and sign in.{note}"
                )
            else:
                await self._add_system_message(f"[bold red]Failed to start server:[/bold red] {msg}")
            return

        # 2. /server close or /server stop
        if subcmd in ("close", "stop"):
            success, msg = stop_server()
            if success:
                await self._add_system_message("[bold yellow]Server closed.[/bold yellow] Incoming connections stopped.")
            else:
                await self._add_system_message(f"[yellow]{msg}[/yellow]")
            return

        # 3. /server scan — interactive network scan + connect flow
        if subcmd in ("scan", "--discover", "discover", "-d"):
            await self._run_server_scan_flow()
            return

        # 4. /server sign-out
        if subcmd in ("sign-out", "signout", "logout"):
            if not self._is_remote_session():
                await self._add_system_message("You are not signed into any remote server.")
                return
            sess = self.remote_session or {}
            self.remote_session = None
            self._update_status_model()
            await self._add_system_message(
                f"[bold green]Signed out[/bold green] of {sess.get('host')}:{sess.get('port')} (user: {sess.get('username')}). "
                "This device is back to normal mode."
            )
            return

        # 5. /server config — host-side auth-mode setting
        if subcmd == "config":
            if len(args) > 1:
                mode = args[1].lower().strip()
                ok, msg = set_auth_mode(mode)
                color = "green" if ok else "red"
                await self._add_system_message(f"[{color}]{msg}[/{color}]")
                if ok:
                    desc = {
                        AUTH_MODE_OPEN: "New users pick Sign up -> enter login + password (+ confirm) and are saved immediately, then asked to Sign In.",
                        AUTH_MODE_APPROVAL: "New users pick Sign up -> credentials are stored in a pending queue. They can only sign in after you approve them with /server user --approvals.",
                        AUTH_MODE_EMAIL: "Reserved for email verification. Signups will see: 'Feature is not available yet. Please contact support or the host for further validation'.",
                    }.get(mode, "")
                    if desc:
                        await self._add_system_message(f"[dim]{desc}[/dim]")
                return

            cfg = get_server_config()
            mode = cfg.get("auth_mode", AUTH_MODE_OPEN)
            pending = list_pending_users()
            lines = [
                "[bold cyan]Server Auth Configuration[/bold cyan]",
                f"• Signup auth mode: [bold]{mode}[/bold]",
                f"• Pending signups awaiting approval: [bold]{len(pending)}[/bold]",
                "\n[bold]Options:[/bold]",
                "  • [green]open[/green]      - Anyone can sign up and use the server immediately",
                "  • [yellow]approval[/yellow] - Signups wait for your approval (/server user --approvals)",
                "  • [red]email[/red]       - Email verification (planned; currently blocks signups with a 'not available yet' message)",
                "\nUsage: [cyan]/server config <open|approval|email>[/cyan]",
            ]
            await self._add_system_message("\n".join(lines))
            return

        # 6. /server user ... (host-side account management)
        if subcmd == "user":
            if len(args) == 1:
                users = list_users()
                lines = [f"[bold cyan]Approved Users ({len(users)}):[/bold cyan]"]
                for u in users:
                    lines.append(f"  • [bold]{u}[/bold]")
                lines.append("\nUsage: /server user <name> <password>  |  /server user rm <name>  |  /server user --approvals")
                await self._add_system_message("\n".join(lines))
                return

            # /server user --approvals — view & manage pending signups
            if args[1].lower() in ("--approvals", "approvals", "pending"):
                pending = list_pending_users()
                if not pending:
                    await self._add_system_message("[green]No pending signups awaiting approval.[/green]")
                    return
                import time as _time
                lines = [f"[bold yellow]Pending Signups ({len(pending)}):[/bold yellow]"]
                for u, info in pending.items():
                    ts = info.get("requested_at", 0)
                    age = f" ({int((_time.time() - ts) // 60)} min ago)" if ts else ""
                    lines.append(f"  • [bold]{u}[/bold]{age}")
                lines.append("\nApprove with: [cyan]/server user approve <name>[/cyan]   Deny with: [cyan]/server user deny <name>[/cyan]")
                await self._add_system_message("\n".join(lines))
                return

            if args[1].lower() in ("approve", "deny") and len(args) > 2:
                target = args[2]
                if args[1].lower() == "approve":
                    if approve_user(target):
                        await self._add_system_message(f"[bold green]User '[bold]{target}[/bold]' approved.[/bold green] They can now sign in to your server.")
                    else:
                        await self._add_system_message(f"[red]No pending signup found for '{target}'.[/red]")
                else:
                    if deny_user(target):
                        await self._add_system_message(f"[bold yellow]Pending signup for '{target}' denied.[/bold yellow] They cannot sign in.")
                    else:
                        await self._add_system_message(f"[red]No pending signup found for '{target}'.[/red]")
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
                set_user(u_name, u_pw)
                await self._add_system_message(
                    f"[bold green]User saved successfully![/bold green]\n"
                    f"• Username: [bold]{u_name}[/bold]\n"
                    f"[dim](Host-created accounts are approved immediately; signup-mode rules apply only to self-service signups.)[/dim]"
                )
                return

            await self._add_system_message("Usage: [cyan]/server user <username> <password>[/cyan]  |  [cyan]/server user --approvals[/cyan]")
            return

        # 7. /server users
        if subcmd == "users":
            users = list_users()
            pending = list_pending_users()
            lines = [f"[bold cyan]Approved Users ({len(users)}):[/bold cyan]"]
            for u in users:
                lines.append(f"  • [bold]{u}[/bold]")
            if pending:
                lines.append(f"\n[bold yellow]Pending approvals ({len(pending)}):[/bold yellow] " + ", ".join(pending.keys()))
                lines.append("[dim]Review with /server user --approvals[/dim]")
            await self._add_system_message("\n".join(lines))
            return

        # 8. /server discoverable <on|off>
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

        # 9. /server --connect <host[:port]> [username] [password]
        if subcmd in ("--connect", "-c", "connect"):
            await self._run_server_connect_flow(
                args[1] if len(args) > 1 else "",
                args[2] if len(args) > 2 else "",
                args[3] if len(args) > 3 else "",
            )
            return

        # 10. /server remotes
        if subcmd == "remotes":
            remotes = list_remote_servers()
            if not remotes:
                await self._add_system_message("No remote servers connected. Use [cyan]/server scan[/cyan] or [cyan]/server --connect <host:port>[/cyan]")
            else:
                lines = [f"[bold cyan]Connected Remote Servers ({len(remotes)}):[/bold cyan]"]
                for name, info in remotes.items():
                    cached = info.get("cached_models", [])
                    lines.append(f"  • [bold]{name}[/bold] ({info.get('url')}) - Models: {len(cached)}")
                await self._add_system_message("\n".join(lines))
            return

        await self._add_system_message(
            f"Unknown server subcommand '[bold]{subcmd}[/bold]'.\n"
            f"Options: [cyan]start, close, scan, --connect, sign-out, config, user, users, discoverable, remotes[/cyan]"
        )

    # ------------------------------------------------------------------
    # Remote-server connect / scan flows (interactive, prompt-box driven)
    # ------------------------------------------------------------------

    async def _probe_remote_info(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        """GET /v1/info on a candidate host; None when unreachable."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"http://{host}:{port}/v1/info")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("app") == "phantombox":
                        return data
        except Exception:
            pass
        return None

    async def _run_server_scan_flow(self) -> None:
        """'/server scan' — discover nearby PhantomBox servers and offer to connect."""
        from phantombox.server import discover_lan_servers

        spinner = await self._start_status("Scanning network…")
        try:
            found = await asyncio.to_thread(discover_lan_servers, 2.0)
        finally:
            await self._stop_status(spinner)

        if not found:
            await self._add_system_message(
                "[bold red]The preferred network is either not open or the entered IP is unreachable, please try again later[/bold red]\n"
                "[dim]Note: VPS/cloud servers are reachable over the internet by ip:port, not by LAN scan — use the manual address below.[/dim]"
            )
            await self._run_server_connect_flow("")
            return

        lines = [f"[bold green]Found {len(found)} PhantomBox server(s) on this network:[/bold green]"]
        for idx, s in enumerate(found, 1):
            dtype = s.get("device_type", "unknown")
            lines.append(
                f"  [{idx}] [bold]{s.get('server_name')}[/bold] — [cyan]{s.get('ip')}:{s.get('port')}[/cyan] "
                f"[dim]({dtype})[/dim]"
            )
        lines.append("\nType the number to connect, or press enter to cancel.")
        await self._add_system_message("\n".join(lines))

        choice = await self._ask_input("Select a server [1-{}] (empty = cancel):".format(len(found)))
        if not choice or not choice.isdigit() or not (1 <= int(choice) <= len(found)):
            await self._add_system_message("[dim]Scan cancelled.[/dim]")
            return
        target = found[int(choice) - 1]
        await self._run_server_connect_flow(f"{target.get('ip')}:{target.get('port')}")

    async def _run_server_connect_flow(self, hostport: str, preset_user: str = "", preset_pass: str = "") -> None:
        """Interactive connect: probe host -> menu (Sign In / Help / Exit) -> auth -> session."""
        target = (hostport or "").replace("http://", "").replace("https://", "").strip()
        if not target:
            # No target given: show live scan results so the user can pick one
            await self._add_system_message("Specify a host, or run [cyan]/server scan[/cyan] to find servers on this network.")
            return
        if ":" in target:
            host, port_str = target.split(":", 1)
            port = int(port_str) if port_str.isdigit() else 8443
        else:
            host, port = target, 8443

        spinner = await self._start_status(f"Connecting to {host}:{port}…")
        try:
            info = await self._probe_remote_info(host, port)
        finally:
            await self._stop_status(spinner)

        if info is None:
            await self._add_system_message(
                "[bold red]The preferred network is either not open or the entered IP is unreachable, please try again later[/bold red]"
            )
            return

        # Connection is open: show the connect menu
        signup_mode = info.get("signup_mode", "open")
        device_type = info.get("device_type", "unknown")
        is_cloud = info.get("is_cloud", False)
        cloud_note = (
            "\n[bold yellow]VPS/cloud server[/bold yellow] — this box is reachable over the internet by its public ip:port, not just on LAN."
            if is_cloud else ""
        )
        await self._add_system_message(
            f"[bold green]Connected to[/bold green] [bold]{info.get('name', host)}[/bold] [dim]({device_type}, signup: {signup_mode})[/dim]"
            f"{cloud_note}\n"
            f"Host: [cyan]{info.get('ip', host)}:{info.get('port', port)}[/cyan]\n\n"
            "[bold]Options:[/bold]\n"
            "  1. Sign In\n"
            "  2. Help\n"
            "  3. Exit\n"
            f"\n[dim]Server IP: {info.get('ip', host)}  •  Port: {info.get('port', port)}[/dim]"
        )
        choice = await self._ask_input("Choose an option (1/2/3):", default="3")

        if choice == "2":
            await self._add_system_message(
                "[bold cyan]Remote connection help[/bold cyan]\n"
                "• Sign In — use an account that already exists on the host server.\n"
                "• New users: pick Sign Up inside the Sign In flow; the host's signup rules decide what happens next.\n"
                "• While connected you can chat with the host's models and search routes — everything else is read-only.\n"
                "• Leave the session any time with [cyan]/server sign-out[/cyan]."
            )
            return
        if choice != "1":
            await self._add_system_message("[dim]Connect cancelled.[/dim]")
            return

        # --- Sign In branch: Saved user vs New user ---
        await self._add_system_message(
            "[bold]Sign In:[/bold]\n"
            "  1. Saved User\n"
            "  2. New user"
        )
        sub = await self._ask_input("Choose (1/2):", default="1")

        if sub == "2":
            await self._run_remote_signup_flow(host, port, signup_mode)
            return

        # Saved user: username then password, retried on failure
        username = preset_user or await self._ask_input("Username:")
        if not username:
            await self._add_system_message("[dim]Sign-in cancelled.[/dim]")
            return
        password = preset_pass or await self._ask_input("Password:")
        if not password:
            await self._add_system_message("[dim]Sign-in cancelled.[/dim]")
            return
        await self._remote_sign_in(host, port, username, password)

    async def _remote_sign_in(self, host: str, port: int, username: str, password: str) -> None:
        """Authenticate against the host and activate the read-only remote session."""
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"http://{host}:{port}/v1/auth/login",
                    json={"username": username, "password": password},
                )
        except Exception as exc:
            await self._add_system_message(
                f"[bold red]The preferred network is either not open or the entered IP is unreachable, please try again later[/bold red]\n[dim]({exc})[/dim]"
            )
            return

        if resp.status_code == 403:
            await self._add_system_message(
                "[bold yellow]Your credentials are still awaiting pending approval. Please contact the network host.[/bold yellow]"
            )
            return
        if resp.status_code == 401:
            await self._add_system_message("[bold red]Incorrect username or password.[/bold red] Please try again with /server --connect.")
            return
        if resp.status_code != 200:
            await self._add_system_message(f"[bold red]Sign-in failed (HTTP {resp.status_code}):[/bold red] {resp.text[:200]}")
            return

        data = resp.json()
        token = data.get("token", "")
        if not token:
            await self._add_system_message("[bold red]Sign-in failed:[/bold red] the server did not return a session token.")
            return

        self.remote_session = {
            "host": host,
            "port": port,
            "username": data.get("username", username),
            "url": f"http://{host}:{port}",
            "token": token,
        }
        self._update_status_model()
        await self._add_system_message(
            f"[bold green]Connection established![/bold green] Signed in as [bold]{data.get('username', username)}[/bold] "
            f"on [cyan]{host}:{port}[/cyan].\n"
            "[dim]Remote mode: you can chat with the host's models and search routes. Settings, MCP and provider "
            "commands are locked — use /server sign-out to leave."
        )

    async def _run_remote_signup_flow(self, host: str, port: int, signup_mode: str) -> None:
        """'New user' -> Sign up: collect credentials, then apply the host's auth mode."""
        if signup_mode == "email":
            await self._add_system_message(
                "[yellow]Feature is not available yet. Please contact support or the host for further validation[/yellow]"
            )
            return

        await self._add_system_message("[bold]Sign up[/bold] — create an account on the host server")
        username = await self._ask_input("New login:")
        if not username:
            await self._add_system_message("[dim]Sign up cancelled.[/dim]")
            return
        password = await self._ask_input("New password:")
        if not password:
            await self._add_system_message("[dim]Sign up cancelled.[/dim]")
            return
        confirm = await self._ask_input("Confirm password:")
        if confirm != password:
            await self._add_system_message("[bold red]Passwords do not match. Sign up cancelled.[/bold red]")
            return

        # Email mode would ask for an email here; that path is not active yet.
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(
                    f"http://{host}:{port}/v1/auth/signup",
                    json={"username": username, "password": password, "password_confirm": confirm},
                )
        except Exception as exc:
            await self._add_system_message(
                f"[bold red]The preferred network is either not open or the entered IP is unreachable, please try again later[/bold red]\n[dim]({exc})[/dim]"
            )
            return

        if resp.status_code == 501:  # email mode on the host
            await self._add_system_message(
                "[yellow]Feature is not available yet. Please contact support or the host for further validation[/yellow]"
            )
            return
        if resp.status_code == 409:
            await self._add_system_message(f"[bold red]{resp.json().get('message', 'Username already taken.')}[/bold red]")
            return
        if resp.status_code == 202:
            await self._add_system_message(
                "[yellow]Your request has been sent to the network host for approval. Please try logging in later. "
                "If you still can't log in, refer to the error message and contact the network host.[/yellow]"
            )
            return
        if resp.status_code == 201:
            data = resp.json()
            await self._add_system_message(
                "[bold green]Account created successfully! You can now log in[/bold green]\n"
                "[dim]Choose 'Sign In' to continue.[/dim]"
            )
            # Straight into sign-in with the just-created credentials
            username2 = await self._ask_input("Username:", default=username)
            pw2 = await self._ask_input("Password:")
            if pw2:
                await self._remote_sign_in(host, port, username2, pw2)
            return
        await self._add_system_message(f"[bold red]Sign up failed (HTTP {resp.status_code}):[/bold red] {resp.text[:200]}")

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

    async def _send_route_status(self, mode_name: str, active: bool, on_desc: str, off_desc: str) -> None:
        """Report the ON/OFF state of a single routing mode."""
        suffix = self._get_router_mode_suffix()
        tag = f"phantombox/routes{suffix}"
        state = "[bold green]ON[/bold green]" if active else "[bold red]OFF[/bold red]"
        desc = on_desc if active else off_desc
        await self._add_system_message(
            f"[bold cyan]Route Mode:[/bold cyan] {mode_name} — {state}\n"
            f"{desc}\n"
            f"Prompt tag shows: [bold]{tag}[/bold]"
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
            from phantombox.router import (
                scan_all_routes,
                exclude_route,
                include_route,
                set_shuffle_mode,
                is_shuffle_mode,
                set_overdrive_model,
                clear_overdrive_model,
                get_overdrive_model,
                set_provider_filter,
                clear_provider_filter,
                get_provider_filter,
                get_excluded_models,
            )

            rest = [a.lower() for a in args[1:]]
            sub = rest[0] if rest else ""

            # Validate the subcommand up front. Previously anything unrecognised
            # (e.g. `--routes bogus`) fell through to a full route scan, which
            # looked like the argument had been accepted.
            _VALID_ROUTE_SUBS = {
                "", "--status", "status", "--rescan", "rescan", "refresh", "--all", "all",
                "exclude", "include", "excluded", "exclusions", "shuffle", "overdrive",
                "provider", "providers",
            }
            if sub and sub not in _VALID_ROUTE_SUBS and not sub.startswith("--"):
                await self._add_system_message(
                    f"[yellow]Unknown argument '{sub}'.[/yellow] Valid: "
                    "[cyan]--status | rescan [provider] | exclude <model> | include <model> | "
                    "shuffle [on|off|--status] | overdrive <model|off|--status> | "
                    "provider <name|off|--list|--status>[/cyan]"
                )
                return

            # ---- aggregate --status : every routing mode at once ----
            if sub == "--status" or sub == "status":
                from phantombox.router import get_overdrive_model as _god, get_provider_filter as _gpf, is_shuffle_mode as _ism, get_excluded_models as _gem
                od = _god()
                pf = _gpf()
                sh = _ism()
                ex = _gem()
                tag = f"phantombox/routes{self._get_router_mode_suffix()}"
                lines = [
                    "[bold cyan]PhantomBox Route Mode Status[/bold cyan]",
                    f"• Prompt tag: [bold]{tag}[/bold]",
                    f"• Shuffle: {'[bold green]ON[/bold green]' if sh else '[dim]off[/dim]'}",
                    f"• Overdrive: {'[bold green]ON[/bold green] (' + od + ')' if od else '[dim]off[/dim]'}",
                    f"• Provider filter: {'[bold green]ON[/bold green] (' + pf + ')' if pf else '[dim]off[/dim]'}",
                    f"• Excluded routes: {'[bold yellow]' + str(len(ex)) + '[/bold yellow]' if ex else '[dim]none[/dim]'}",
                    "\n[dim]Toggle with /phantombox --routes shuffle|overdrive <model>|provider <name>, each accepts off / --status.[/dim]",
                ]
                if ex:
                    lines += [f"    - {e}" for e in ex]
                await self._add_system_message("\n".join(lines))
                return

            # ---- exclude --status / include --status ----
            if sub in ("exclude", "include") and len(rest) > 1 and rest[1] in ("--status", "status"):
                excluded = get_excluded_models()
                if excluded:
                    lines = [f"[bold cyan]Excluded routes ({len(excluded)}):[/bold cyan]"]
                    lines += [f"  • {e}" for e in excluded]
                    await self._add_system_message("\n".join(lines))
                else:
                    await self._add_system_message("No routes are excluded — all verified routes are active.")
                return

            # ---- exclude <model> ----
            if sub == "exclude":
                if len(args) < 3:
                    await self._add_system_message(
                        "[yellow]A model is required.[/yellow] Usage: "
                        "[cyan]/phantombox --routes exclude <model>[/cyan] "
                        "(or [cyan]exclude --status[/cyan] to list current exclusions)."
                    )
                    return
                ok, msg = exclude_route(args[2])
                self._update_status_model()
                style = "green" if ok else "yellow"
                await self._add_system_message(f"[{style}]{msg}[/{style}]")
                return

            # ---- include <model> (with 200 OK ping verification) ----
            if sub == "include":
                if len(args) < 3:
                    await self._add_system_message(
                        "[yellow]A model is required.[/yellow] Usage: "
                        "[cyan]/phantombox --routes include <model>[/cyan] "
                        "(see exclusions with [cyan]exclude --status[/cyan])."
                    )
                    return
                await self._add_system_message(f"[dim]Pinging '{args[2]}' to verify it still returns 200 OK…[/dim]")
                ok, msg = await asyncio.to_thread(include_route, args[2])
                style = "green" if ok else "yellow"
                await self._add_system_message(f"[{style}]{msg}[/{style}]")
                return

            # ---- shuffle / shuffle off / shuffle --status ----
            if sub == "shuffle":
                if len(rest) > 1 and rest[1] in ("--status", "status"):
                    await self._send_route_status("shuffle", is_shuffle_mode(), "ON — every call picks a random verified model (only one verified model = always that model)", "OFF — routes are ordered fastest-first")
                    return
                turning_off = len(rest) > 1 and rest[1] in ("off", "disable", "no")
                set_shuffle_mode(not turning_off)
                if not turning_off:
                    # Shuffle means a fresh random pick every call: drop the
                    # mid-turn pin so the previous winner can't be preferred.
                    from phantombox.providers import clear_pinned_router_model
                    clear_pinned_router_model()
                self._update_status_model()
                state = "disabled" if turning_off else "ENABLED"
                await self._add_system_message(
                    f"[cyan]Route shuffle mode {state}.[/cyan] "
                    + ("Routes fall back to fastest-first ordering." if turning_off else "Every call now randomly picks one of the verified routes (falling back to the next random route if it errors).")
                    + f"\n[dim]Prompt tag now shows: phantombox/routes{self._get_router_mode_suffix() or ''}[/dim]"
                )
                return

            # ---- overdrive <model> / overdrive off / overdrive --status ----
            if sub == "overdrive":
                if len(rest) > 1 and rest[1] in ("--status", "status"):
                    current = get_overdrive_model()
                    await self._send_route_status("overdrive", bool(current), f"ON — locked to {current} until its quota dies", "OFF — adaptive fastest-first routing")
                    return
                if len(rest) > 1 and rest[1] in ("off", "disable", "none", "no"):
                    clear_overdrive_model()
                    self._update_status_model()
                    await self._add_system_message("[cyan]Overdrive disengaged.[/cyan] Routing returns to adaptive fastest-first.")
                    return
                if len(args) > 2:
                    ok, msg = set_overdrive_model(args[2])
                    if ok:
                        from phantombox.providers import clear_pinned_router_model
                        clear_pinned_router_model()
                    self._update_status_model()
                    style = "green" if ok else "yellow"
                    suffix_note = f"\n[dim]Prompt tag now shows: phantombox/routes{self._get_router_mode_suffix() or ''}[/dim]" if ok else ""
                    await self._add_system_message(f"[{style}]{msg}[/{style}]{suffix_note}")
                    return
                current = get_overdrive_model()
                await self._add_system_message(
                    f"Overdrive is currently: [bold]{current or 'OFF'}[/bold].\n"
                    f"Usage: [cyan]/phantombox --routes overdrive <model_name>[/cyan] to lock onto one model, add [cyan]off[/cyan] to release, or [cyan]--status[/cyan] to check."
                )
                return

            # ---- provider <name> / provider off / provider --list ----
            if sub == "provider" or (rest and rest[0] == "providers"):
                # provider --list (also tolerate the 'roviders --list' typo)
                if len(rest) > 1 and rest[1] in ("--status", "status"):
                    current = get_provider_filter()
                    await self._send_route_status("provider", bool(current), f"ON — only routing through '{current}'", "OFF — all connected providers considered")
                    return
                if len(rest) > 1 and rest[1] in ("--list", "list", "--ls"):
                    from phantombox.providers import list_connected_providers, get_provider
                    conn = [p for p in list_connected_providers() if p != "phantombox"]
                    active = get_provider_filter()
                    counts: dict[str, int] = {}
                    for v in get_verified_models():
                        pid_v = (v.get("provider") or "").lower()
                        counts[pid_v] = counts.get(pid_v, 0) + 1
                    lines = ["[bold cyan]Connected Providers:[/bold cyan]"]
                    if not conn:
                        lines.append("  [dim](none — connect one with /providers --connect)[/dim]")
                    for p in conn:
                        mark = " [bold green](active filter)[/bold green]" if p == active else ""
                        info = get_provider(p) or {}
                        n = counts.get(p.lower(), 0)
                        routes = f"{n} verified route(s)" if n else "[yellow]no verified routes — run rescan[/yellow]"
                        lines.append(f"  • [bold]{p}[/bold] ({info.get('name', p)}) — {routes}{mark}")
                    current = get_provider_filter()
                    lines.append(f"\nActive provider filter: [bold]{current or 'OFF'}[/bold]")
                    lines.append(
                        "[dim]Use these exact names (or 'Claude'/'Groq'/'NVIDIA' style tags) with:\n"
                        "  /phantombox --routes rescan <name>\n"
                        "  /phantombox --routes provider <name>[/dim]"
                    )
                    await self._add_system_message("\n".join(lines))
                    return
                if len(rest) > 1 and rest[1] in ("off", "disable", "none", "no"):
                    clear_provider_filter()
                    self._update_status_model()
                    await self._add_system_message("[cyan]Provider filter disabled.[/cyan] Routing again considers all connected providers.")
                    return
                if len(args) > 2:
                    # Resolve + (when needed) live-probe off the UI thread so the
                    # interface keeps animating while the provider is verified.
                    ok, msg = await asyncio.to_thread(set_provider_filter, args[2])
                    self._update_status_model()
                    style = "green" if ok else "yellow"
                    suffix_note = f"\n[dim]Prompt tag now shows: phantombox/routes{self._get_router_mode_suffix() or ''}[/dim]" if ok else ""
                    await self._add_system_message(f"[{style}]{msg}[/{style}]{suffix_note}")
                    return
                # bare 'provider' -> status
                current = get_provider_filter()
                await self._add_system_message(
                    f"Provider filter is currently: [bold]{current or 'OFF'}[/bold].\n"
                    f"Usage: [cyan]/phantombox --routes provider <provider_name>[/cyan] (add [cyan]off[/cyan] to disable, or [cyan]--status[/cyan] to check), "
                    f"or [cyan]/phantombox --routes provider --list[/cyan] to see connected providers."
                )
                return

            # ---- excluded --list ----
            if sub in ("excluded", "exclusions"):
                excluded = get_excluded_models()
                if excluded:
                    lines = [f"[bold cyan]Excluded routes ({len(excluded)}):[/bold cyan]"]
                    lines += [f"  • {e}" for e in excluded]
                    lines.append("\n[dim]Re-include with: /phantombox --routes include <model> (verified by a live 200 OK ping)[/dim]")
                    await self._add_system_message("\n".join(lines))
                else:
                    await self._add_system_message("No routes are currently excluded.")
                return

            force_rescan = sub in ("--rescan", "rescan", "refresh")
            # Targeted rescan: /phantombox --routes rescan <provider>
            scan_provider = None
            if force_rescan and len(args) > 2 and args[2].lower() not in ("--all", "all"):
                scan_provider = args[2]
                force_rescan = True
            verified = get_verified_models()

            if force_rescan or not verified:
                # Animated "Scanning Routes" spinner while the full probe runs
                spinner = SpinnerThinking(color="#38bdf8", label_text="Scanning Routes…")
                warning = Static(
                    "[red]⚠ If you have many MCP servers connected, it might take a while to ping all of the models[/red]",
                    id="scan-warning",
                )
                try:
                    chat_view = self.query_one("#chat-view", VerticalScroll)
                    await chat_view.mount(spinner)
                    await chat_view.mount(warning)
                    chat_view.scroll_end(animate=False)
                except Exception:
                    spinner = None
                    warning = None

                try:
                    if scan_provider:
                        # Resolve the name exactly as --list prints it. A name that
                        # resolves is always treated as found; only the scan result
                        # decides between 'no models' and 'no 200 OK'.
                        from phantombox.router import _resolve_connected_provider
                        from phantombox.providers import fetch_cached_or_default_models
                        resolved_pid = _resolve_connected_provider(scan_provider)
                        if not resolved_pid:
                            from phantombox.providers import list_connected_providers
                            conn = [p for p in list_connected_providers() if p != "phantombox"]
                            await self._add_system_message(
                                f"[yellow]'{scan_provider}' is not a connected provider. "
                                f"Connected: {', '.join(conn) if conn else 'none'}.[/yellow]\n"
                                f"[dim]Run [cyan]/phantombox --routes provider --list[/cyan] for the exact names, "
                                f"or [cyan]/providers --connect[/cyan] to add a provider.[/dim]"
                            )
                            return
                        await self._add_system_message(
                            f"[dim]Targeted scan: pinging only [bold]{resolved_pid}[/bold] models…[/dim]"
                        )
                        verified = await scan_all_routes(provider_filter=resolved_pid, force_catalog=True)
                        catalog = fetch_cached_or_default_models(resolved_pid)
                        if not catalog:
                            await self._add_system_message(
                                f"[yellow]No models found for provider '{resolved_pid}'.[/yellow]\n"
                                f"Its model catalog endpoint returned nothing (invalid/expired API key, no access, "
                                f"or a provider without a catalog endpoint). Check the key via [cyan]/providers --connect[/cyan]."
                            )
                            return
                        if not verified:
                            await self._add_system_message(
                                f"[yellow]No 200 OK routes for provider '{resolved_pid}'.[/yellow]\n"
                                f"{len(catalog)} model(s) were found but none answered a probe "
                                f"(quota exhausted, rate limited, or unsupported models)."
                            )
                            return
                    else:
                        verified = await scan_all_routes(force_catalog=True)
                finally:
                    for w in (spinner, warning):
                        if w:
                            try:
                                await w.remove()
                            except Exception:
                                pass
            else:
                # Instantly show the last scan; a background re-scan refreshes silently
                await self._add_system_message(
                    "[dim]Showing last verified routes. Add [cyan]rescan[/cyan] to force a fresh ping of every model "
                    "(or [cyan]rescan <provider>[/cyan] to ping just one provider)."
                )

            if not verified:
                await self._add_system_message(
                    "[yellow]No working routes found. Check that your provider API keys have valid quota via [cyan]/providers --connect[/cyan].[/yellow]"
                )
                return

            lines = [
                "[bold cyan]PhantomBox Verified Routes[/bold cyan] "
                f"({len(verified)} models returning 200 OK):",
            ]
            for v in verified:
                lat = v.get("last_latency", 0)
                lat_str = f"{lat:.2f}s" if lat >= 1.0 else f"{int(lat*1000)}ms"
                from phantombox.capabilities import supports_tools
                cap = supports_tools(str(v.get('provider') or ""), str(v.get('model_id') or ""))
                cap_str = "" if cap is not False else " [yellow](no tools)[/yellow]"
                lines.append(f"  • [bold]{v.get('provider')}/{v.get('model_id')}[/bold] — [green]{lat_str}[/green]{cap_str}")
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
                f"• Probe Timeout per Model: [dim]{cfg.get('timeout_per_attempt', 6.0)}s[/dim]",
                f"• Verified 200 OK Models ({len(verified)}):",
            ]
            if verified:
                for v in verified:
                    p = v.get("provider")
                    m = v.get("model_id")
                    count = v.get("success_count", 1)
                    lat = v.get("last_latency", 0)
                    lines.append(f"    - [bold]{p}/{m}[/bold] (pings: {count}, last: {lat}s)")
            else:
                lines.append("    [dim](No models benchmarked yet. Switch to 'phantombox/router' in /models to start auto-routing!)[/dim]")

            lines.append("\n[bold]Router Commands:[/bold]")
            lines.append("  • [cyan]/phantombox --routes[/cyan] - Show every cloud model currently returning 200 OK")
            lines.append("  • [cyan]/phantombox --routes rescan[/cyan] - Re-ping all cloud models now and rebuild the verified route table")
            lines.append("  • [cyan]/phantombox --routes rescan <provider>[/cyan] - Faster: re-ping only one provider's models (e.g. rescan gemini)")
            lines.append("  • [cyan]/phantombox --routes exclude <model>[/cyan] - Explicitly exclude a model from the route list ([cyan]--status[/cyan] to check)")
            lines.append("  • [cyan]/phantombox --routes include <model>[/cyan] - Re-include an excluded model (verified via live 200 OK ping)")
            lines.append("  • [cyan]/phantombox --routes shuffle[/cyan] - Random verified model on every call (add [cyan]off[/cyan] / [cyan]--status[/cyan])")
            lines.append("  • [cyan]/phantombox --routes overdrive <model>[/cyan] - Commit to one model until quota dies (add [cyan]off[/cyan] / [cyan]--status[/cyan])")
            lines.append("  • [cyan]/phantombox --routes provider <name>[/cyan] - Only route through a specific provider (add [cyan]off[/cyan] / [cyan]--status[/cyan] / [cyan]--list[/cyan])")
            lines.append("  • [cyan]/phantombox --routes --status[/cyan] - Show every routing mode's ON/OFF state and the current prompt tag")
            lines.append("  • [dim]Prompt tag reflects the active mode: phantombox/routes → /shuffle, /<provider>, or /overdrive[/dim]")
            lines.append("  • [cyan]/models phantombox/router[/cyan] - Switch active chat model to Smart Router")
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
            "  • [cyan]/phantombox --routes rescan <provider>[/cyan] - Faster targeted re-ping of just one provider\n"
            "  • [cyan]/phantombox --routes exclude|include|shuffle|overdrive|provider ...[/cyan] - Advanced route controls\n"
            "  • [cyan]/phantombox --routes --status[/cyan] - Show every routing mode's ON/OFF state\n"
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
                from phantombox.capabilities import tool_capability_label
                cap = tool_capability_label(pid, mid)
                cap_str = "" if cap == "tools: yes" else f" [dim]({cap})[/dim]"
                label = f"● [{tag}] {mid}{cap_str}{curr_str}"
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

    async def _open_models_picker_async(self) -> None:
        """Load every connected provider's live catalog, then open /models.

        Previously /models opened instantly from whatever happened to be in the
        in-memory cache, so a fresh chat session showed only local models until
        the background warm-up finished (or forever, if it had failed). Now the
        catalogs are fetched concurrently right before the picker opens.
        """
        from phantombox.providers import list_connected_providers, fetch_provider_models

        pids = [p for p in list_connected_providers() if p != "phantombox"]
        if pids:
            self._switch_to_chat()
            status = None
            try:
                chat_view = self.query_one("#chat-view", VerticalScroll)
                status = SpinnerThinking(color="#38bdf8", label_text="Loading provider models…")
                await chat_view.mount(status)
                chat_view.scroll_end(animate=False)
            except Exception:
                status = None
            try:
                await asyncio.gather(*(fetch_provider_models(pid) for pid in pids))
            except Exception:
                pass
            finally:
                if status is not None:
                    try:
                        await status.remove()
                    except Exception:
                        pass

        self._open_models_picker()

    @staticmethod
    def _resolve_valid_model_argument(target: str, available: list[tuple[str, str, bool]]) -> tuple[str | None, str]:
        """Map a user-typed /models argument onto a real, selectable model.

        Returns (canonical_id, "") on success or (None, reason) on failure.

        Arguments are only ever accepted when they identify a model the user is
        actually allowed to pick: exact id, ``provider/model``, ``provider:model``,
        a bare model id, or a router mode tag (``phantombox/router/shuffle``).
        Everything else is rejected instead of being silently turned into a
        llama.cpp start attempt for a model that does not exist.
        """
        raw = (target or "").strip()
        if not raw:
            return None, "No model given."

        lower = raw.lower()
        ids = [mid for mid, _label, _ok in available]
        lower_map = {mid.lower(): mid for mid in ids}

        # 1. Exact identifier as the picker uses it.
        if lower in lower_map:
            return lower_map[lower], ""

        # 2. Router modes with an optional suffix (shuffle / overdrive / provider).
        from phantombox.providers import split_router_mode_suffix

        base, mode = split_router_mode_suffix(raw)
        if base.lower() in ("phantombox/router", "phantombox/routes", "router"):
            if not mode:
                return "phantombox/router", ""
            if mode == "shuffle":
                from phantombox.router import set_shuffle_mode

                set_shuffle_mode(True)
                return "phantombox/router", ""
            if mode == "overdrive":
                # Overdrive needs a model: the user must supply one.
                return None, (
                    "'/models phantombox/router/overdrive' needs a model. Use "
                    "[cyan]/phantombox --routes overdrive <model>[/cyan] and then "
                    "[cyan]/models phantombox/router/overdrive[/cyan]."
                )
            from phantombox.router import set_provider_filter

            ok, msg = set_provider_filter(mode)
            if not ok:
                return None, msg
            return "phantombox/router", ""

        # 3. 'provider:model' / 'provider/model' spelled with a known provider.
        from phantombox.providers import get_provider

        provider_part, sep, rest = raw.partition(":") if ":" in raw else raw.partition("/")
        p_info = get_provider(provider_part.lower()) if sep else None
        if p_info and rest:
            candidate = f"{p_info['id']}:{rest}"
            if candidate.lower() in lower_map:
                return lower_map[candidate.lower()], ""
            return None, (
                f"'{rest}' is not in {p_info.get('name', p_info['id'])}'s catalog. "
                "Run [cyan]/models[/cyan] to pick from the available models."
            )

        # 4. Bare model id that belongs to a connected provider.
        suffix_hits = [mid for mid in ids if mid.lower().endswith(f":{lower}")]
        if len(suffix_hits) == 1:
            return suffix_hits[0], ""
        if len(suffix_hits) > 1:
            listed = ", ".join(suffix_hits[:6])
            return None, (
                f"'{raw}' exists on more than one provider. Be specific: {listed}"
            )

        # 5. Local model names (picker shows them unqualified).
        if any(mid.lower() == lower for mid, _l, _o in available if not mid.startswith(("phantombox", "server/", "remote/"))):
            return lower, ""

        return None, (
            f"'{raw}' is not a valid model. Run [cyan]/models[/cyan] to choose from the "
            "available local and connected cloud models."
        )

    @staticmethod
    def _split_router_imprint(text: str) -> tuple[str, bool]:
        """Strip a textual 'Response returned by …' footer out of streamed text.

        Returns (clean_text, had_imprint). Attribution is rendered once, from
        router_meta, so any footer embedded in the body is removed here.
        """
        if "Response returned by:" not in text:
            return text, False
        marker = text.find("Response returned by:")
        clean = text[:marker]
        # Drop the '---' rule and blank lines that precede the footer.
        clean = clean.rstrip()
        while clean.endswith("---"):
            clean = clean[:-3].rstrip()
        return clean, True

    def _imprint_line(self) -> str:
        """Single attribution line for the last routed reply (empty if none)."""
        meta = getattr(self, "_last_router_meta", {}) or {}
        if not meta or meta.get("overdrive_failed"):
            return ""
        model = meta.get("model")
        if not model:
            return ""
        sub = " [bold cyan](subagent1)[/bold cyan]" if meta.get("subagents_mode") else ""
        return (
            f"[dim]Response returned by:[/dim] [bold cyan]{model}[/bold cyan] "
            f"[dim]via[/dim] [bold #f97316]phantombox[/bold #f97316]{sub} "
            f"[dim]after[/dim] [bold green]{meta.get('time', '')}[/bold green]"
        )

    async def _switch_to_model(self, target_name: str) -> None:
        """Switch active model, supporting local llama servers and connected cloud providers."""
        self._switch_to_chat()
        target_key = target_name.strip()

        # Validate against the REAL selectable set before doing anything. Without
        # this, any string was accepted and then handed to the local-server
        # starter, which failed with a confusing 'could not start' error.
        try:
            available = self._get_all_available_models()
        except Exception:
            available = []
        resolved, reason = self._resolve_valid_model_argument(target_key, available)
        if resolved is None:
            await self._add_system_message(f"[yellow]{reason}[/yellow]")
            return
        if resolved != target_key:
            target_key = resolved
            self._update_status_model()

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
                    f"Run [cyan]/providers --connect[/cyan] to connect your API key."
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
            f"Please ensure it is downloaded or connect a provider with [cyan]/providers --connect[/cyan]."
        )

    async def _send_user_message(self, text: str) -> None:
        self._record_history(text)
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
        # Attribution for THIS turn only (a stale value must never leak through).
        self._last_router_meta = {}

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
        # Defensive cleanup: if a previous turn left a stale thinking/spinner row
        # in the view (e.g. after an unhandled worker crash), remove it first so
        # mounting a new one can never raise DuplicateIds.
        try:
            for stale_row in chat_view.query(".role-assistant#thinking-row, .role-assistant[id^='thinking-row']"):
                await stale_row.remove()
        except Exception:
            pass
        try:
            stale_spinner = chat_view.query_one("#active-stream-spinner")
            await stale_spinner.remove()
        except Exception:
            pass

        # Unique per-turn id: two turns can never collide on the same widget id.
        self._thinking_seq = getattr(self, "_thinking_seq", 0) + 1
        thinking_row_id = f"thinking-row-{self._thinking_seq}"

        asst_header = Static(f"● {self.assistant_name}", classes="message-header")
        if self.assistant_color:
            asst_header.styles.color = self.assistant_color

        thinking_container = Container(
            asst_header,
            SpinnerThinking(color=self.assistant_color),
            id=thinking_row_id,
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
        # Tool-loop budget is MODEL-DRIVEN: the model decides how many rounds of
        # tool calls it needs and keeps going until it answers with pure text.
        # max_tool_loops is only a hard safety guard against a runaway loop —
        # not a quota. If the guard trips, one final synthesis call still runs
        # (see below) so the user always gets an actual answer.
        max_tool_loops = 32

        # Tool capability gate: sending tool schemas to a model that cannot use
        # them either does nothing (silently ignored) or, on some endpoints,
        # returns a hard 400. Decide once per turn and tell the user why.
        #
        # Tool schemas come from the manager's in-memory cache, so this is fast;
        # but if a warm-up MCP connect pass is still running we wait for it here
        # (off the UI thread) so the user sees exactly what is happening instead
        # of a generic spinner.
        spinner_lbl = self._current_thinking_container
        if spinner_lbl is not None:
            try:
                spinner_lbl.query_one(SpinnerThinking).set_label(self._tool_prep_label())
            except Exception:
                pass
        turn_tools = await asyncio.to_thread(self._tools_for_active_model)
        if spinner_lbl is not None:
            try:
                spinner_lbl.query_one(SpinnerThinking).set_label("Thinking…")
            except Exception:
                pass
        if self._tools_disabled_notice_pending:
            self._tools_disabled_notice_pending = False
            await self._add_system_message(
                f"[yellow]This model ({self.model_name}) does not support tool calling.[/yellow] "
                "It will answer from its own knowledge only — no file, terminal or MCP tools.\n"
                "[dim]Switch to a tool-capable model with /models (look for 'tools: yes'), or use /phantombox --routes.[/dim]"
            )

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
                    turn_tools,
                ):
                    if self._stop_requested:
                        break

                    # Router attribution arrives as router_meta, not as text.
                    # The fallback branch of the router *also* appended a plain
                    # 'Response returned by ...' footer into the stream, so a
                    # routed turn rendered TWO imprints (one generated by the
                    # message body, a second printed separately by the caller).
                    # Capture the meta, strip any textual footer, and show exactly
                    # one attribution line.
                    meta = delta.get("router_meta")
                    if meta:
                        self._last_router_meta = meta

                    raw_content = delta.get("content")
                    if raw_content:
                        content_chunk, _had_imprint = self._split_router_imprint(raw_content)
                    else:
                        content_chunk = raw_content

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
                    content_chunk = content_chunk if content_chunk else None
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
                    # Purge every leftover spinner so nothing keeps animating
                    try:
                        await chat_view.query_one("#active-stream-spinner").remove()
                    except Exception:
                        pass
                    try:
                        await thinking_container.remove()
                    except Exception:
                        pass
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
                    try:
                        stale_row = chat_view.query_one("#thinking-row-1")
                        await stale_row.remove()
                    except Exception:
                        pass
                    asst_header = Static(f"● {self.assistant_name}", classes="message-header")
                    if self.assistant_color:
                        asst_header.styles.color = self.assistant_color
                    thinking_container = Container(
                        asst_header,
                        SpinnerThinking(color=self.assistant_color, label_text="Synthesizing…"),
                        id="thinking-row-1",
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

                # Exactly one attribution line for a routed turn (see
                # _split_router_imprint: the textual footer was already stripped).
                imprint = self._imprint_line()
                if imprint and current_assistant_msg is not None:
                    try:
                        line = current_assistant_msg.query_one("#message-imprint-line", Static)
                        line.update(imprint)
                        line.styles.display = "block"
                    except Exception:
                        pass

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
                # Remove EVERY leftover spinner (thinking row + streaming spinner)
                # so nothing animates after the model finishes or hits an error.
                for leftover_id in ("#active-stream-spinner",):
                    try:
                        await chat_view.query_one(leftover_id).remove()
                    except Exception:
                        pass
                try:
                    await thinking_container.remove()
                except Exception:
                    pass
                self._current_thinking_container = None
                if not self._stop_requested:
                    await self._add_system_message(f"[red]Error communicating with model: {e}[/red]")
                break

        self._is_generating = False
        self._current_thinking_container = None
        self._update_status_model()

        # Model-driven tool loops (max_tool_loops is only a runaway guard): if the
        # guard tripped while the last message is still a tool result, run ONE
        # final synthesis call so the user always receives an actual answer.
        if loop_count >= max_tool_loops and self.messages and self.messages[-1].get("role") == "tool" and not self._stop_requested:
            try:
                thinking_container = Container(
                    Static(f"● {self.assistant_name}", classes="message-header"),
                    SpinnerThinking(color=self.assistant_color, label_text="Synthesizing…"),
                    id="thinking-row-1",
                    classes="message-row role-assistant",
                )
                try:
                    stale_row = chat_view.query_one("#thinking-row-1")
                    await stale_row.remove()
                except Exception:
                    pass
                await chat_view.mount(thinking_container)
                chat_view.scroll_end(animate=False)

                full_reply = ""
                first_token = True
                current_assistant_msg: ChatMessage | None = None
                tool_calls_dict: Dict[int, Dict[str, Any]] = {}

                # stream_chat_deltas is imported at module level; do NOT re-import
                # it here — a function-local import would shadow the global name
                # for the entire function scope and raise UnboundLocalError in
                # the main streaming loop above.
                async for delta in stream_chat_deltas(
                    model_identifier=self.model_name,
                    local_port=self.port,
                    messages=self.messages,
                    tools=self.tools,
                ):
                    # Safety-guard synthesis stays pure text: any further tool
                    # calls are dropped here (this branch only runs when the
                    # runaway guard tripped, never in normal model-driven flow).
                    if delta.get("tool_calls"):
                        continue
                    content = delta.get("content")
                    if not content:
                        continue
                    if first_token:
                        first_token = False
                        try:
                            await thinking_container.remove()
                        except Exception:
                            pass
                        current_assistant_msg = ChatMessage(self.assistant_name, "", "assistant", sender_color=self.assistant_color)
                        await chat_view.mount(current_assistant_msg)
                    if current_assistant_msg:
                        await current_assistant_msg.append_content(content)
                    full_reply += content

                if not full_reply:
                    if first_token:
                        try:
                            await thinking_container.remove()
                        except Exception:
                            pass
                        current_assistant_msg = ChatMessage(self.assistant_name, "(empty response)", "assistant", sender_color=self.assistant_color)
                        await chat_view.mount(current_assistant_msg)

                from phantombox.subagents import clean_message_for_subagent
                self.messages.append({"role": "assistant", "content": clean_message_for_subagent(full_reply)})
            except Exception:
                pass

    async def _ask_user_permission(self, fn_name: str, fn_args: Dict[str, Any]) -> tuple[bool, str]:
        """Prompt user in chat log and await yes/y/no/n or a custom reply."""
        from phantombox.tools import describe_tool_call
        from phantombox.mcp import is_mcp_tool

        args_repr = json.dumps(fn_args, indent=2)
        is_external = is_mcp_tool(fn_name)

        if is_external:
            server = fn_name.split("__", 1)[0]
            header = (
                f"[bold red]External tool[/bold red] [bold {self.theme_color}]{fn_name}[/bold {self.theme_color}] "
                f"from MCP server [bold]{server}[/bold] wants to run.\n"
                f"[dim]This code runs on a third-party server, not inside PhantomBox.[/dim]\n"
                f"[bold]What it does:[/bold] {describe_tool_call(fn_name)}\n"
            )
        else:
            header = (
                f"The assistant wants to execute tool [bold {self.theme_color}]{fn_name}[/bold {self.theme_color}] "
                f"in phantombox-dir with arguments:\n"
            )

        confirm_text = (
            f"{header}"
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
