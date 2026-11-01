# phantombox.ai-cli

Local-first offline AI runtime.

## Install

```bash
pipx install git+https://github.com/taremwastudios/phantombox.ai-cli.git

Usage
phantombox setup
phantombox pull <model-url>
phantombox chat <model>


## Tools & Internet Search

Active models in phantombox chat have access to local workspace tools and live internet search:
- **`internet_search`**: Live DuckDuckGo search returning page titles, URLs, and summaries. Models use this to retrieve fresh facts, verify technical documentation, or check current events to prevent hallucination. Search results are injected directly to the model context and not dumped as raw output to the user. Instead, an indicator such as `🔍 Searched 7 pages` is displayed in chat.
- **`list_dir`**: List directory contents in the scoped `phantombox-dir`.
- **`read_file`**: Read file contents inside `phantombox-dir`.
- **`create_file`**: Create a file with full initial content.
- **`create_folder`**: Create folders inside `phantombox-dir`.
- **`write_file`**: Overwrite or write complete content to a file.
- **`edit_file`**: In-place text replacement in an existing file.
- **`run_command`**: Execute a shell or terminal process safely inside `phantombox-dir`.

### How Models Use Tools
Smaller models can be prone to hallucination (e.g. inventing tool outputs or claiming to have saved files when they only printed markdown code blocks). Phantombox enforces strict system prompt instructions:
1. **Always use standard function calls**: Models must invoke tool schemas rather than simulating text outputs.
2. **Execute when writing code**: When asked to save or write code, models must execute `create_file` or `write_file` with the full content.
3. **Verify before assuming**: Models inspect files using `list_dir` and `read_file` instead of guessing file contents.
4. **Internet search for facts**: When uncertain about facts, documentation, or recent events, models call `internet_search`.
5. **Run terminal processes**: When executing tests, scripts, builds, or system processes, models invoke `run_command`.

### Settings & Controls
Inside chat, you can configure permissions, search, thinking, and generation controls:
- `/settings` - Open interactive permissions & tool scope settings
- `/settings search <on|off>` - Toggle live internet search
- `/stop` (or press `Esc`) - Stop the model from answering immediately
- `/models --thinking <yes|no>` - Toggle deep reasoning / thinking mode (or `/thinking yes|no`)
- Sending a new message while the model is replying automatically interrupts the previous answer and starts processing the new prompt.

## MCP & External Model Providers

Connect cloud AI model providers directly into Phantombox chat:
- **Command**: Run `/mcp --connect` in chat to open the external provider connection modal.
- **Supported Providers**:
  - **OpenRouter**: Access 200+ models (Claude 3.7/3.5, GPT-4o, Llama 3.3, DeepSeek R1, Qwen 2.5)
  - **Groq**: Ultra-fast LPU inference (Llama 3.3 70B, DeepSeek R1 Distill, Mixtral)
  - **Anthropic Claude**: Claude 3.7 Sonnet, Claude 3.5 Sonnet, Claude 3.5 Haiku
  - **OpenAI**: GPT-4o, GPT-4o-mini, o1, o3-mini
  - **Google Gemini**: Gemini 2.0 Flash, Gemini 1.5 Pro & Flash
  - **Qwen / DashScope**: Qwen-Max, Qwen-Plus, Qwen2.5-Coder
  - **DeepSeek**: DeepSeek-V3, DeepSeek-R1
  - **Mistral AI**: Mistral Large, Codestral

### How to Connect & Switch Models
1. In chat, type `/mcp --connect`.
2. Select any provider from the list and press `Enter`.
3. Paste your API key and press `Enter` to save (or click Disconnect to remove a key).
4. Type `/models` to open the model picker. The connected provider's model catalog will be populated alongside your local models.
5. Select any model to switch to it immediately. The prompt box footer status indicator will update to show the active model (e.g. `● openrouter/claude-3.7-sonnet` or `● groq/llama-3.3-70b-versatile`).


### Expanded Providers
The following additional providers are now available via `/mcp --connect`:
- **AionLabs**: Specialized AI cloud inference (`aionlabs`)
- **Baseten**: Truss and vLLM production endpoints (`baseten`)
- **Azure AI**: Azure OpenAI and Model Catalog (`azure`)
- **Amazon Bedrock**: AWS Bedrock foundational models (`bedrock`)
- **Atlas Cloud**: Serverless GPU AI inference (`atlascloud`)
- **AssemblyAI**: LeMUR LLM speech-to-reasoning engine (`assemblyai`)
- **AkashML**: Decentralized GPU inference (`akashml`)
- **Grok (xAI)**: Grok-2 and Grok-2 Vision (`grok`)
- **Baidu Qianfan**: ERNIE-4.0 and Qianfan models (`qianfan`)

### Chat Command & Context Usage Marker
- Run `phantombox chat` without arguments to launch directly. If a local model is running, it connects automatically; otherwise, it opens with `(no model selected)`.
- Real-time context window usage is displayed right after the active model name (e.g. `● groq/llama-3.3-70b-versatile 1k/131k (1%)`).
  - `< 10%`: **Green**
  - `<= 50%`: **Yellow**
  - `>= 90%`: **Red**
- Run `/clear` to reset conversation and clear context usage back to initial system prompt tokens.


## LAN AI Server & Mobile Receiver Architecture (/server)

PhantomBox features a built-in LAN/Hotspot AI Server that exposes your local and cloud AI models to mobile phones (Android / iOS) and other computers on the same network or mobile hotspot.

### Features
1. **Three-Tier User Privileges**:
   - **`all`**: Full administrative access. Can modify server configurations, create/delete users, alter system prompts, tool permissions, and chat with all models.
   - **`regular`**: Custom standard access. Can chat with models, configure user-level chat preferences (names, themes), but cannot touch server administrative/security settings or user management.
   - **`use-only`**: Strict model interaction only. Can send requests and chat with models; zero permissions to modify any settings.
2. **Network Discovery**:
   - Automatic UDP Broadcast Beacon (port 8444) allows mobile clients on the same WiFi or hotspot to auto-discover the desktop server without manually typing IP addresses.
   - Can be toggled on/off with `/server discoverable <on|off>`.
3. **Cross-Server Chaining**:
   - Use `/server --connect <host:port> [user] [pass]` to link multiple laptops and desktops together, sharing model catalogs across machines.

### Commands in Chat:
- `/server`: View current server status, LAN IP address, registered users, and discovery state.
- `/server start [port]`: Start the LAN server (default port 8443).
- `/server close` (or `/server stop`): Stop and shut down the server.
- `/server user <username> <password> <all|regular|use-only>`: Create or update a user with privilege level.
- `/server user rm <username>`: Delete a user.
- `/server users`: List all registered users.
- `/server discoverable <on|off>`: Toggle network broadcast discovery.
- `/server --discover`: Scan the local network for other PhantomBox servers.
- `/server --connect <host:port> <user> <pass>`: Connect to another PhantomBox server.
- `/server remotes`: List connected remote servers.

### CLI Commands:
- `phantombox server start [--port 8443]`
- `phantombox server close`
- `phantombox server user <username> <password> [--privilege all|regular|use-only]`
- `phantombox server users`
- `phantombox server status`


## Subagents Architecture (/subagents)

PhantomBox supports configuring specialized subagents (local or cloud models) with custom nicknames, roles, and automated helper scripts:

### Subagent Commands:
- `/subagents` or `/subagents --help`: Show the subagents command guide.
- `/subagents setup <nickname> <model> [role]`: Register or update a subagent with a unique nickname and designated model.
- `/subagents scripts`: View available automated helper scripts (e.g. `suggestions`, `web_assistant`, `code_reviewer`, `terminal_operator`).
- `/subagents scripts <nickname> <script1,script2>`: Assign scripts to a subagent.
- `/subagents --list`: View all registered subagents.
- `/subagents rm <nickname>`: Remove a subagent.
- `/subagents --global auto <nickname>`: Automatically assign a subagent as a helper across all sessions.
- `/subagents auto <nickname>`: Attach a subagent to assist the current chat session.
- Models can autonomously delegate tasks, terminal executions, or file operations to subagents via the built-in `call_subagent(nickname, task)` tool.

## Chat Session Persistence (/chat)

- `/chat save [name]`: Save the current chat conversation into a persistent session file.
- `/chat restore`: Opens an interactive session dialog listing all saved chats. Pressing `Enter` restores the chosen session, and pressing `d` deletes the highlighted session from disk.

## Model Tools: Move & Delete Files
- **`move_file(source_path, destination_path)`**: Move or rename any file or directory inside `phantombox-dir`.
- **`delete_file(path)`**: Delete a file or directory inside `phantombox-dir`.


## PhantomBox Autonomous Smart Router (`phantombox/router`)

The Smart Router is PhantomBox's signature zero-friction intelligence engine. Instead of manually testing dozens of cloud models or providers to see which keys have quota or return a `200 OK`, users select `phantombox/router`:

### How It Works:
1. **Zero-Key Entry**: Listed under `/mcp --connect` as the `PhantomBox Router` (requires no API key).
2. **Automated Ping & Fallback**:
   - Gathers all connected cloud providers (OpenRouter, Groq, Claude, OpenAI, Gemini, Qwen, DeepSeek, Mistral, etc.).
   - Dispatches requests across candidate models until a responsive `200 OK` model is found.
3. **Adaptive Learning Algorithm**:
   - Every time a model successfully returns a response, its provider, model ID, latency, and success count are cached.
   - When verified models exist in the cache, the router tries up to **10 exploratory calls** before immediately falling back to the assured working models.
   - If multiple verified models exist, calls are distributed to minimize response times.
4. **Attribution Imprint**:
   - Displays the winning model and latency below the response:
     `Response returned by: openrouter/deepseek/deepseek-chat via phantombox after 1.4s`
5. **Router Management (`/phantombox --router`)**:
   - `/phantombox --router`: View active router strategy, verified models, and latency benchmarks.
   - `/phantombox --router attempts <N>`: Adjust max exploratory attempts before guaranteed fallback.
   - `/phantombox --router timeout <seconds>`: Adjust per-model probe timeout (default: 6.0s).
   - `/phantombox --router clear`: Reset router latency and model history.


## PhantomBox Subagents Mode (`phantombox/subagents`)

In addition to `phantombox/router`, PhantomBox provides `phantombox/subagents` as a selectable model entry:
- **Router + Autonomous Helper Subagent**: Runs the autonomous Smart Router to find a responsive 200 OK model, while automatically provisioning and attaching a helper subagent (default: `subagent1` with `suggestions` script) using one of the verified 200 OK endpoints.
- **Provider Consistency**: The router locks onto the winning model for subsequent tool-call iterations within the turn, ensuring that tool calls and syntheses are executed by the exact same model unless an error occurs.
- **Persistent Streaming Spinner**: If a model experiences generation lag or latency between token chunks, an active `Generating…` spinner remains visible directly below the streaming message until completion.


## Smart Router Resiliency & Cloud Exhaustion Fallback

- **Local Model Auto-Fallback**: If all configured cloud providers fail or exhaust credits/quotas during routing, PhantomBox automatically falls back to any running local offline model (e.g. `qwen2.5-0.5b`) without dropping the user's prompt.
- **Actionable Diagnostic Prompt**: If no local model is currently running when cloud quotas are depleted, PhantomBox outputs an actionable system diagnostic advising the user how to spin up a local zero-cost model (`phantombox start <model>`) or pull a lightweight offline model (`phantombox pull qwen2.5-0.5b`).
- **Clean Imprint Styling**: The attribution footer (`Response returned by: ... via phantombox after Xs`) is rendered with bold cyan for the model name, orange for phantombox, and green for latency.
