"""Modern chat UI using Textual – OpenCode terminal style."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import httpx
from rich.markdown import Markdown
from textual import events, work
from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Container, VerticalScroll, Horizontal
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static, Label, OptionList, TextArea
from textual.widgets.option_list import Option

from phantombox.runtime import list_running, start_model
from phantombox.models import list_downloaded_models, RECOMMENDED_MODELS
from phantombox.config import (
    load_config,
    set_config_val,
    reset_config_val,
    DEFAULT_CONFIG,
)
from phantombox.tools import (
    TOOL_DEFINITIONS,
    check_tool_permission,
    execute_tool,
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
    """Modal screen for switching models (shows ONLY downloaded models)."""

    DEFAULT_CSS = """
    ModelPickerModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.75);
    }

    #picker-container {
        width: 76;
        max-height: 22;
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
        max-height: 12;
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
    ]

    def __init__(self, current_model: str, options: list[tuple[str, str, bool]]):
        super().__init__()
        self.current_model = current_model
        self.options = options
        self._initial_idx = 0

    def compose(self) -> ComposeResult:
        with Container(id="picker-container"):
            yield Static("Downloaded Models", id="picker-title")
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
                yield Static("No downloaded models found on disk.\nRun 'phantombox pull <name>' first.", id="picker-empty")
                yield Static("Esc: Close", id="picker-help")

    def on_mount(self) -> None:
        if self.options:
            ol = self.query_one("#picker-options", OptionList)
            ol.highlighted = self._initial_idx
            ol.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.id))


class SettingsModal(ModalScreen[None]):
    """Modal screen for configuring permission levels and path scopes."""

    DEFAULT_CSS = """
    SettingsModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.8);
    }

    #settings-container {
        width: 84;
        max-height: 26;
        background: #161b22;
        border: heavy #f97316;
        padding: 1 2;
    }

    #settings-title {
        color: #f97316;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }

    #settings-options {
        background: #0d1117;
        border: tall #30363d;
        height: auto;
        max-height: 14;
    }

    #settings-help {
        color: #8b949e;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Container(id="settings-container"):
            yield Static("Settings: Tools & Permissions", id="settings-title")
            yield OptionList(id="settings-options")
            yield Static("Enter: Toggle / Select  •  Esc: Save & Close", id="settings-help")

    def on_mount(self) -> None:
        self._refresh_options()

    def _refresh_options(self) -> None:
        cfg = load_config()
        mode = cfg.get("permission_mode", "restricted")
        reads = cfg.get("allowed_read_paths", ["*"])
        writes = cfg.get("allowed_write_paths", ["*"])
        custom_auto = cfg.get("custom_auto_approve_tools", ["list_dir", "read_file"])

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

        # Scopes
        read_scope_str = "All files (*)" if "*" in reads else ", ".join(reads)
        write_scope_str = "All files (*)" if "*" in writes else ", ".join(writes)
        ol.add_option(Option(f"  • Read Scope: {read_scope_str} (Enter to toggle All vs .)", id="toggle_read"))
        ol.add_option(Option(f"  • Write Scope: {write_scope_str} (Enter to toggle All vs .)", id="toggle_write"))

        # Custom tool permissions
        if mode == "custom":
            for tool_name in ["list_dir", "read_file", "write_file", "edit_file"]:
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

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        opt_id = str(event.option.id)
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
        elif opt_id.startswith("tool_"):
            tool_name = opt_id.replace("tool_", "")
            custom_auto = list(cfg.get("custom_auto_approve_tools", []))
            if tool_name in custom_auto:
                custom_auto.remove(tool_name)
            else:
                custom_auto.append(tool_name)
            set_config_val("custom_auto_approve_tools", custom_auto)

        self._refresh_options()


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
            yield Static(Markdown(self.content), classes="message-body")
        else:
            yield Static(self.content, classes="message-body")

    def update_content(self, new_content: str) -> None:
        self.content = new_content
        body = self.query_one(".message-body", Static)
        if self.role == "assistant":
            body.update(Markdown(new_content))
        else:
            body.update(new_content)


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

    /* Thinking spinner block */
    #thinking-row {
        height: auto;
        margin: 1 0;
        padding: 0 1;
    }

    .thinking-text {
        color: #f97316;
        text-style: bold;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+q", "quit", "Quit"),
    ]

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

        self.messages: List[dict] = [
            {"role": "system", "content": "You are a helpful assistant with local tools (list_dir, read_file, write_file, edit_file). Use them whenever asked to inspect or edit files or directories."}
        ]
        self.in_welcome_mode = True

        # Tool confirmation future for pause/resume
        self._pending_confirmation: Optional[asyncio.Future[str]] = None

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
                        yield Label(f"● {self.model_name}", id="welcome-model-tag", classes="box-model-tag")
                        yield Static(classes="box-footer-spacer")
                        yield Label("Run /help for commands", classes="box-hint-tag")
                yield Static(
                    "[dim]/settings[/dim] Permissions  •  [dim]/models[/dim] Switch model  •  [dim]/user[/dim]  •  [dim]/assistant[/dim]  •  [dim]/color[/dim]",
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
                    yield Label(f"● {self.model_name}", id="dock-model-tag", classes="box-model-tag")
                    yield Static(classes="box-footer-spacer")
                    yield Label("Run /help for commands", classes="box-hint-tag")

    def on_mount(self) -> None:
        self._apply_border_color(self.theme_color)
        self.query_one("#welcome-prompt", PromptArea).focus()

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
        for selector in ("#welcome-model-tag", "#dock-model-tag"):
            try:
                lbl = self.query_one(selector, Label)
                lbl.update(f"● {self.model_name}")
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

    async def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        raw_text = event.value.strip()
        if not raw_text:
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

        if cmd in ("/settings", "/config"):
            await self._handle_settings_command(args)
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

        if cmd == "/models":
            if raw_arg:
                await self._switch_to_model(raw_arg)
            else:
                self._open_models_picker()
            return

        if cmd in ("/clear", "/new"):
            self.messages = [
                {"role": "system", "content": "You are a helpful assistant with local tools (list_dir, read_file, write_file, edit_file)."}
            ]
            chat_view = self.query_one("#chat-view")
            await chat_view.remove_children()
            self._switch_to_chat()
            await self._add_system_message("Session cleared.")
            return

        if cmd == "/help":
            self._switch_to_chat()
            help_text = (
                "Available commands:\n"
                "  • /settings - Open interactive permissions & tool scope settings\n"
                "  • /settings mode <restricted|custom|full_access> - Set permission mode\n"
                "  • /settings read <path|*> - Set allowed folder/file path for reading\n"
                "  • /settings write <path|*> - Set allowed folder/file path for editing/writing\n"
                "  • /models - Switch between downloaded models\n"
                "  • /user <name> - Set user name for this session\n"
                "  • /user --color <color> - Set user display name color (e.g. /user --color cyan)\n"
                "  • /user --global <name> - Save user name permanently across sessions\n"
                "  • /assistant <name> - Set assistant name for this session\n"
                "  • /assistant --color <color> - Set assistant display name color\n"
                "  • /color <color> - Change prompt border color\n"
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

        self._open_settings_modal()

    def _open_settings_modal(self) -> None:
        def on_closed(_) -> None:
            target_prompt = (
                self.query_one("#welcome-prompt", PromptArea)
                if self.in_welcome_mode
                else self.query_one("#dock-prompt", PromptArea)
            )
            target_prompt.focus()

        self.push_screen(SettingsModal(), on_closed)

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

    def _get_downloaded_models(self) -> list[tuple[str, str, bool]]:
        """Collect ONLY downloaded models on disk."""
        downloaded_paths = list_downloaded_models()
        running = list_running()

        options: list[tuple[str, str, bool]] = []
        seen = set()

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

            size_mb = p.stat().st_size / (1024 * 1024)
            size_str = f"{size_mb / 1024:.1f} GB" if size_mb >= 1024 else f"{size_mb:.0f} MB"

            port_str = ""
            if is_running and canonical_name in running:
                port_str = f" (port {running[canonical_name].get('port', 8080)})"
            curr_str = " [CURRENT]" if is_current else ""
            run_dot = "●" if is_running else "○"

            label = f"{run_dot} {canonical_name} ({size_str}){port_str}{curr_str}"
            options.append((canonical_name, label, is_running))

        return options

    def _open_models_picker(self) -> None:
        """Show an interactive modal picker to choose ONLY among downloaded models."""
        options = self._get_downloaded_models()

        def on_selected(choice: str | None) -> None:
            target_prompt = (
                self.query_one("#welcome-prompt", PromptArea)
                if self.in_welcome_mode
                else self.query_one("#dock-prompt", PromptArea)
            )
            target_prompt.focus()

            if choice and choice != self.model_name:
                self.run_worker(self._switch_to_model(choice))

        self.push_screen(ModelPickerModal(self.model_name, options), on_selected)

    async def _switch_to_model(self, target_name: str) -> None:
        """Switch active model, starting server if needed."""
        self._switch_to_chat()
        target_key = target_name.strip()
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
            f"Please ensure it is downloaded: [cyan]phantombox models --list[/cyan]"
        )

    async def _send_user_message(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
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
            loop_count += 1
            full_reply = ""
            current_assistant_msg: ChatMessage | None = None
            first_token = True
            tool_calls_dict: Dict[int, Dict[str, Any]] = {}

            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    async with client.stream(
                        "POST",
                        f"http://127.0.0.1:{self.port}/v1/chat/completions",
                        json={
                            "messages": self.messages,
                            "tools": TOOL_DEFINITIONS,
                            "stream": True,
                            "temperature": 0.7,
                        },
                    ) as response:
                        response.raise_for_status()

                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                                delta = chunk["choices"][0]["delta"]

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

                                    full_reply += content_chunk
                                    if current_assistant_msg:
                                        current_assistant_msg.update_content(full_reply)
                                    chat_view.scroll_end(animate=False)
                            except Exception:
                                continue

                # End of stream chunk processing
                # If tool calls were made by model:
                if tool_calls_dict:
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
                            await self._add_system_message(f"[red]Tool '{fn_name}' denied: {reason}[/red]")
                        elif req == "needs_confirmation":
                            # Ask user for permission
                            confirmed, custom_reply = await self._ask_user_permission(fn_name, fn_args)
                            if confirmed:
                                tool_result = execute_tool(fn_name, fn_args)
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

                self.messages.append({"role": "assistant", "content": full_reply})
                break

            except Exception as e:
                try:
                    await thinking_container.remove()
                except Exception:
                    pass
                await self._add_system_message(f"[red]Error communicating with model: {e}[/red]")
                break

    async def _ask_user_permission(self, fn_name: str, fn_args: Dict[str, Any]) -> tuple[bool, str]:
        """Prompt user in chat log and await yes/y/no/n or a custom reply."""
        args_repr = json.dumps(fn_args, indent=2)
        confirm_text = (
            f"The assistant wants to execute tool [bold #f97316]{fn_name}[/bold #f97316] with arguments:\n"
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


def run_chat(model_name: str) -> None:
    running = list_running()
    if model_name not in running:
        from rich.console import Console
        console = Console()
        console.print(f"[red]Model '{model_name}' is not running.[/red]")
        console.print(f"Start it first with: [cyan]phantombox start {model_name}[/cyan]\n")
        return

    port = running[model_name]["port"]
    app = PhantomChat(model_name, port)
    app.run()
