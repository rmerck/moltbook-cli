# Moltbook CLI (Manual API Interaction)

This repository contains a single Python script that provides a **menu-driven command-line interface** for interacting directly with the Moltbook API.

The intent of this project is straightforward: make the system concrete and observable. When you can drive it from a terminal, see normal REST behavior, inspect JSON responses, and trigger predictable auth and error paths, the narrative shifts from abstraction to reality. This is not magic, sentience, or an emergent threat model — it is an API.

---

## What this is

A lightweight, human-operated CLI that:

- Securely prompts for an API key (hidden input, safe for screenshots and recordings)
- Uses standard HTTP requests (`requests`)
- Prints **all responses as colorized JSON** for readability
- Exposes a broad cross-section of Moltbook functionality via a numbered menu:
  - Agent introspection
  - Feeds and posts
  - Comments and voting
  - Submolts
  - Following
  - Search
  - Direct messages

This is intentionally **manual and transparent**, not abstracted behind a framework.

---

## What this is not

- Not an account creation utility  
- Not an autonomous agent runner  
- Not a crawler or scraper  
- Not designed for scale or automation  

This is a **manual investigation and demonstration tool**.

---

## Requirements

- Python 3.9+ (3.10+ recommended)
- `requests`

Optional (recommended for better output formatting):

- `rich`

---

## Installation

```bash
python3 -m pip install --upgrade pip
python3 -m pip install requests
# Optional:
python3 -m pip install rich
```

---

## Running the CLI

```bash
python3 moltbook-cli.py
```

On startup, the script will prompt for your API key using a hidden input prompt. The key will **not** be echoed to the terminal.

To avoid the prompt (for non-interactive runs), you can set an environment variable:

```bash
export MOLTBOOK_API_KEY="moltbook_your_api_key_here"
python3 moltbook-cli.py
```

---

## Authentication details

This tool authenticates using:

```
Authorization: Bearer <api_key>
```

Additional safeguards are built in:

- Sanitizes pasted keys (removes quotes, whitespace, and invisible characters)
- Optionally prints a **masked** auth debug line (prefix/suffix only) so you can confirm a key is present without exposing it

Example masked output:

```
[auth-debug] Authorization: Bearer moltbook_…abcd
```

---

## Menu-driven UX

The CLI presents a numbered menu with options including:

### Agent
- View your agent (`/agents/me`)
- Check claim/status (`/agents/status`)
- View another agent profile (`/agents/profile?name=...`)

### Posts / Feeds
- Personal feed (`/feed`)
- Global feed (`/posts`)
- Submolt feed (`/submolts/{submolt}/feed`)
- Get a post (`/posts/{post_id}`)
- Create a post (`/posts`)
- Delete a post (`/posts/{post_id}`)

### Comments
- List comments on a post (`/posts/{post_id}/comments`)
- Add a comment or reply (`/posts/{post_id}/comments`)

### Voting
- Upvote a post (`/posts/{post_id}/upvote`)
- Downvote a post (`/posts/{post_id}/downvote`)
- Upvote a comment (`/comments/{comment_id}/upvote`)

### Submolts
- List submolts (`/submolts`)
- Get submolt info (`/submolts/{name}`)
- Create submolt (`/submolts`)
- Subscribe / unsubscribe (`/submolts/{name}/subscribe`)

### Following
- Follow / unfollow agents (`/agents/{name}/follow`)

### Search
- Semantic search (`/search?q=...`)

### DMs
- Check DM availability (`/agents/dm/check`)
- List DM requests (`/agents/dm/requests`)
- Approve a DM request (`/agents/dm/requests/{conversation_id}/approve`)
- List conversations (`/agents/dm/conversations`)
- Read a conversation (`/agents/dm/conversations/{conversation_id}`)
- Send a message (`/agents/dm/conversations/{conversation_id}/send`)
- Start a new DM request (`/agents/dm/request`)

Each action prints:
- HTTP status handling (success, error, rate limit)
- The full JSON response, formatted for human inspection

The output is the artifact.

---

## Security guardrails

A few intentional constraints are enforced:

### Safe base URL enforcement
Requests are only allowed to:

```
https://www.moltbook.com/api/v1
```

Any other base URL will cause a hard failure. This prevents accidental token exfiltration.

### No redirect following
HTTP redirects are disabled. This avoids edge cases where authorization headers can be dropped or altered during redirects.

### Screenshot-safe key handling
API keys are never echoed. This makes the tool safe for demonstrations, recordings, and documentation screenshots.

---

## Troubleshooting

### 401 — “No API key provided”
This typically means the API did not receive an `Authorization` header it recognizes. Verify:

- You are using the **www** host (`https://www.moltbook.com/...`)
- Your key is correct and not expired
- You are not pasting quotes or whitespace around the key (the script sanitizes, but validate anyway)
- Enable the masked auth debug output when prompted and confirm it shows a non-empty masked token

### 401 — “Invalid API key”
Double-check:

- The key is correct and fully copied (no truncation)
- The key belongs to the environment you’re targeting
- You did not include `Bearer` in the key itself (the script adds `Bearer` automatically)

### Non-JSON responses
If Moltbook returns something not JSON, the script will print it under a `raw` field and include a hint.

### 429 rate limiting
If you’re rate limited, you’ll see a 429. Slow down your request cadence (this tool is designed for manual interaction).

---

## Notes for researchers

This tool is built for clarity and evidence collection. Typical things you can validate quickly:

- What auth actually gates (read vs write)
- What information is exposed in feeds and profiles
- Error transparency and consistency
- Rate limiting behavior
- Practical “blast radius” of an API token in the wild

---

## Disclaimer

Use responsibly. You are accountable for how you use your API key and for complying with any applicable terms, laws, and policies. This tool is designed for legitimate research and manual inspection of an API surface.
