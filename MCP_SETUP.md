# ScreenMind MCP Server Setup

ScreenMind includes an MCP (Model Context Protocol) server that lets AI assistants like **Claude Desktop**, **Cursor**, **VS Code** (via Cline/Continue), and any MCP-compatible client query your screen history directly.

## What It Does

The MCP server exposes your ScreenMind data as **tools** that AI assistants can call:

| Tool | Description |
|---|---|
| `search_screen` | Search screen history using natural language (semantic + keyword) |
| `get_recent_activity` | Get the most recent N screen activities |
| `get_activity_by_time` | Get activities for a specific date/time range |
| `get_daily_summary` | Get AI-generated daily summary and standup notes |
| `capture_now` | Trigger an instant screenshot capture |
| `get_stats` | Get overall statistics about your screen history |
| `search_audio` | Search meeting transcripts |
| `get_screenshot` | Get a screenshot image for a specific activity |

## Setup

### 1. Install Dependencies

```bash
pip install "screenmind[mcp]"
# from a source checkout: uv sync (the dev group already includes the mcp extra)
```

### 2. Configure Your AI Client

#### Claude Desktop

Edit your Claude Desktop config file:
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "screenmind": {
      "command": "python",
      "args": ["C:/path/to/screenmind/screenmind.mcp_server"]
    }
  }
}
```

Replace `C:/path/to/screenmind/` with the actual path to your ScreenMind installation.

#### Cursor

Go to **Settings → MCP** and add:
```json
{
  "screenmind": {
    "command": "python",
    "args": ["C:/path/to/screenmind/screenmind.mcp_server"]
  }
}
```

#### VS Code (via Cline or Continue)

Add to your MCP configuration in `.vscode/mcp.json`:
```json
{
  "servers": {
    "screenmind": {
      "command": "python",
      "args": ["C:/path/to/screenmind/screenmind.mcp_server"]
    }
  }
}
```

### 3. Restart Your AI Client

After saving the configuration, restart your AI client. You should see "ScreenMind" listed as an available MCP server with 8 tools.

## Testing

You can test the MCP server locally using the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python -m screenmind.mcp_server
```

This opens a web UI at `http://127.0.0.1:6274` where you can test each tool interactively.

## Example Prompts

Once connected, try asking your AI assistant:

- *"What was I working on today?"* → calls `get_recent_activity`
- *"Search my screen for discord messages"* → calls `search_screen`
- *"What did I do between 2pm and 5pm yesterday?"* → calls `get_activity_by_time`
- *"Give me my daily summary"* → calls `get_daily_summary`
- *"Take a screenshot right now"* → calls `capture_now`

## Privacy

- The MCP server runs **locally** on your machine
- Data never leaves your computer — the AI client queries your local SQLite database
- The server is read-only (except `capture_now` which triggers a local screenshot)
- No API keys or cloud services required

## Requirements

- ScreenMind must be installed and have captured some screen data
- For `capture_now` to work, the ScreenMind app must be running (`screenmind` or `python -m screenmind`)
- Python environment with `mcp[cli]` installed
