#!/usr/bin/env python3
"""
Moltbook CLI (manual interaction)
- Single-file, copy/paste runnable
- Menu-driven CLI
- Secure API key input (hidden) for screenshot/video safety
- Prints all API responses as colorized JSON

Dependencies:
  - requests (required)
  - (optional) rich  -> nicer JSON formatting if installed

Notes:
  - Uses Authorization: Bearer <api_key> (per server hint).
  - Refuses unsafe BASE_URLs to prevent accidental key exfiltration.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

try:
    import requests  # type: ignore
except ImportError:
    print("ERROR: Missing dependency 'requests'. Install with: python3 -m pip install requests")
    sys.exit(1)

# -------------------------------
# Configuration
# -------------------------------

BASE_URL = "https://www.moltbook.com/api/v1"
DEFAULT_TIMEOUT = 30
ENV_API_KEY_NAME = "MOLTBOOK_API_KEY"

ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}

# Optional richer output
HAVE_RICH = False
try:
    from rich.console import Console  # type: ignore
    from rich.json import JSON  # type: ignore

    HAVE_RICH = True
    _console = Console()
except Exception:
    HAVE_RICH = False
    _console = None


def _warn(msg: str) -> None:
    print(f"{ANSI['yellow']}WARN{ANSI['reset']}: {msg}")


def _err(msg: str) -> None:
    print(f"{ANSI['red']}ERROR{ANSI['reset']}: {msg}")


def _ok(msg: str) -> None:
    print(f"{ANSI['green']}OK{ANSI['reset']}: {msg}")


def ensure_safe_base_url(base_url: str) -> None:
    if not base_url.startswith("https://www.moltbook.com/api/v1"):
        raise ValueError(
            f"Refusing to use unsafe base URL: {base_url!r}. "
            f"Must be 'https://www.moltbook.com/api/v1...'"
        )


def pause() -> None:
    input(f"\n{ANSI['dim']}Press Enter to continue...{ANSI['reset']}")


def prompt(text: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{text}{suffix}: ").strip()
    return val if val else (default or "")


def prompt_int(
    text: str,
    default: Optional[int] = None,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> int:
    while True:
        raw = prompt(text, str(default) if default is not None else None)
        try:
            n = int(raw)
            if min_val is not None and n < min_val:
                _warn(f"Value must be >= {min_val}")
                continue
            if max_val is not None and n > max_val:
                _warn(f"Value must be <= {max_val}")
                continue
            return n
        except ValueError:
            _warn("Enter a valid integer.")


def prompt_yes_no(text: str, default_yes: bool = False) -> bool:
    d = "y" if default_yes else "n"
    raw = prompt(f"{text} (y/n)", d).lower()
    return raw.startswith("y")


def colorize_json_plain(s: str) -> str:
    """
    Lightweight ANSI colorizer for JSON text (no external deps).
    Colors:
      - keys: cyan
      - strings: green
      - numbers: magenta
      - booleans/null: yellow
      - punctuation: gray
    """
    import re as _re

    s = _re.sub(r'([{}\[\],:])', f"{ANSI['gray']}\\1{ANSI['reset']}", s)

    s = _re.sub(
        r'("([^"\\]|\\.)*")(\s*' + _re.escape(f"{ANSI['gray']}:") + r")",
        lambda m: f"{ANSI['cyan']}{m.group(1)}{ANSI['reset']}{m.group(3)}",
        s,
    )

    s = _re.sub(
        r'("([^"\\]|\\.)*")',
        lambda m: f"{ANSI['green']}{m.group(1)}{ANSI['reset']}",
        s,
    )

    s = _re.sub(
        r'(?<!")\b-?\d+(\.\d+)?\b(?!")',
        lambda m: f"{ANSI['magenta']}{m.group(0)}{ANSI['reset']}",
        s,
    )

    s = _re.sub(
        r"\b(true|false|null)\b",
        lambda m: f"{ANSI['yellow']}{m.group(1)}{ANSI['reset']}",
        s,
    )
    return s


def print_json(data: Any) -> None:
    if HAVE_RICH and _console is not None:
        try:
            from rich.json import JSON  # type: ignore

            _console.print(JSON.from_data(data))
            return
        except Exception:
            pass

    raw = json.dumps(data, indent=2, ensure_ascii=False)
    print(colorize_json_plain(raw))


def sanitize_api_key(raw: str) -> str:
    """
    Normalize keys from paste/clipboard:
    - strip whitespace
    - remove surrounding quotes
    - remove common zero-width/invisible characters
    """
    if raw is None:
        return ""

    s = raw.strip()

    # Remove surrounding quotes if user pasted `"moltbook_..."` or `'moltbook_...'`
    if (len(s) >= 2) and ((s[0] == s[-1]) and s[0] in ("'", '"')):
        s = s[1:-1].strip()

    # Remove zero-width and BOM chars often introduced by clipboard
    s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")

    # Collapse internal whitespace just in case (keys should never contain spaces)
    s = re.sub(r"\s+", "", s)

    return s


def mask_key(key: str) -> str:
    """
    Mask key for debug display: show prefix and suffix only.
    """
    if not key:
        return "<empty>"
    if len(key) <= 12:
        return key[0:2] + "…" + key[-2:]
    return key[:8] + "…" + key[-4:]


def get_api_key() -> str:
    """
    Securely obtain the API key:
      1) From env var MOLTBOOK_API_KEY if set
      2) Otherwise, prompt using getpass (hidden input)
    """
    env_val = os.environ.get(ENV_API_KEY_NAME)
    if env_val and env_val.strip():
        key = sanitize_api_key(env_val)
        if key:
            return key

    raw = getpass.getpass("Enter Moltbook API key (input hidden): ")
    key = sanitize_api_key(raw)
    if not key:
        raise ValueError("API key is required (got empty after sanitization).")
    return key


@dataclass
class MoltbookClient:
    api_key: str
    base_url: str = BASE_URL
    timeout: int = DEFAULT_TIMEOUT
    auth_debug: bool = False

    def __post_init__(self) -> None:
        ensure_safe_base_url(self.base_url)
        if not self.api_key:
            raise ValueError("API key is empty.")
        # FYI only; do not block on prefix because Moltbook keys may vary.
        if not self.api_key.startswith("moltbook_"):
            _warn("API key does not start with 'moltbook_'. Proceeding anyway.")

    def _headers(self, content_type_json: bool = False) -> Dict[str, str]:
        # Per server hint: Authorization: Bearer <api_key>
        h = {"Authorization": f"Bearer {self.api_key}"}
        if content_type_json:
            h["Content-Type"] = "application/json"

        if self.auth_debug:
            # Debug without leaking secret
            print(
                f"{ANSI['dim']}[auth-debug]{ANSI['reset']} "
                f"Authorization: Bearer {mask_key(self.api_key)}"
            )
        return h

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        ensure_safe_base_url(self.base_url)
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path

        # Important: avoid redirects (auth headers can be lost on redirect in some stacks)
        allow_redirects = False

        headers = self._headers(content_type_json=(json_body is not None))
        try:
            resp = requests.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                files=files,
                data=data,
                timeout=self.timeout,
                allow_redirects=allow_redirects,
            )
        except requests.RequestException as e:
            return 0, {"success": False, "error": str(e), "hint": "Check network connectivity and BASE_URL."}

        try:
            payload = resp.json()
            if not isinstance(payload, dict):
                payload = {"success": resp.ok, "data": payload}
        except Exception:
            payload = {
                "success": resp.ok,
                "status_code": resp.status_code,
                "raw": resp.text,
                "hint": "Response was not JSON.",
            }

        payload.setdefault("status_code", resp.status_code)

        if resp.status_code == 429 and isinstance(payload, dict):
            payload.setdefault("hint", "Rate limit hit. See retry_after_* fields if present.")

        if resp.is_redirect or resp.status_code in (301, 302, 307, 308):
            payload.setdefault(
                "warning",
                "Redirect received. Ensure you use https://www.moltbook.com with www.",
            )
            loc = resp.headers.get("Location")
            if loc:
                payload.setdefault("location", loc)

        return resp.status_code, payload

    # -------- Agents --------
    def me(self) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/agents/me")

    def status(self) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/agents/status")

    def profile(self, name: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/agents/profile", params={"name": name})

    # -------- Posts --------
    def get_feed_global(self, sort: str = "hot", limit: int = 25, submolt: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        params: Dict[str, Any] = {"sort": sort, "limit": limit}
        if submolt:
            params["submolt"] = submolt
        return self.request("GET", "/posts", params=params)

    def get_feed_personal(self, sort: str = "hot", limit: int = 25) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/feed", params={"sort": sort, "limit": limit})

    def get_submolt_feed(self, submolt: str, sort: str = "new") -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", f"/submolts/{submolt}/feed", params={"sort": sort})

    def get_post(self, post_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", f"/posts/{post_id}")

    def create_post(self, submolt: str, title: str, content: Optional[str] = None, url: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        body: Dict[str, Any] = {"submolt": submolt, "title": title}
        if content:
            body["content"] = content
        if url:
            body["url"] = url
        return self.request("POST", "/posts", json_body=body)

    def delete_post(self, post_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("DELETE", f"/posts/{post_id}")

    # -------- Comments --------
    def get_comments(self, post_id: str, sort: str = "top") -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", f"/posts/{post_id}/comments", params={"sort": sort})

    def add_comment(self, post_id: str, content: str, parent_id: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        body: Dict[str, Any] = {"content": content}
        if parent_id:
            body["parent_id"] = parent_id
        return self.request("POST", f"/posts/{post_id}/comments", json_body=body)

    # -------- Voting --------
    def upvote_post(self, post_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("POST", f"/posts/{post_id}/upvote")

    def downvote_post(self, post_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("POST", f"/posts/{post_id}/downvote")

    def upvote_comment(self, comment_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("POST", f"/comments/{comment_id}/upvote")

    # -------- Submolts --------
    def list_submolts(self) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/submolts")

    def get_submolt(self, name: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", f"/submolts/{name}")

    def create_submolt(self, name: str, display_name: str, description: str) -> Tuple[int, Dict[str, Any]]:
        body = {"name": name, "display_name": display_name, "description": description}
        return self.request("POST", "/submolts", json_body=body)

    def subscribe_submolt(self, name: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("POST", f"/submolts/{name}/subscribe")

    def unsubscribe_submolt(self, name: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("DELETE", f"/submolts/{name}/subscribe")

    # -------- Following --------
    def follow(self, agent_name: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("POST", f"/agents/{agent_name}/follow")

    def unfollow(self, agent_name: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("DELETE", f"/agents/{agent_name}/follow")

    # -------- Search --------
    def search(self, q: str, search_type: str = "all", limit: int = 20) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/search", params={"q": q, "type": search_type, "limit": limit})

    # -------- DMs --------
    def dm_check(self) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/agents/dm/check")

    def dm_requests(self) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/agents/dm/requests")

    def dm_approve(self, conversation_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("POST", f"/agents/dm/requests/{conversation_id}/approve")

    def dm_conversations(self) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", "/agents/dm/conversations")

    def dm_read_conversation(self, conversation_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("GET", f"/agents/dm/conversations/{conversation_id}")

    def dm_send(self, conversation_id: str, message: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("POST", f"/agents/dm/conversations/{conversation_id}/send", json_body={"message": message})

    def dm_request(self, to: str, message: str) -> Tuple[int, Dict[str, Any]]:
        return self.request("POST", "/agents/dm/request", json_body={"to": to, "message": message})


def print_http_result(status: int, payload: Dict[str, Any]) -> None:
    if status == 0:
        _err("Request failed to send.")
    elif 200 <= status < 300:
        _ok(f"HTTP {status}")
    elif status == 429:
        _warn("HTTP 429 (rate limited)")
    else:
        _err(f"HTTP {status}")
    print_json(payload)


def banner() -> None:
    print(f"{ANSI['bold']}Moltbook CLI{ANSI['reset']}  {ANSI['dim']}manual API interaction{ANSI['reset']}")
    print(f"Base URL: {ANSI['cyan']}{BASE_URL}{ANSI['reset']}")
    print(f"{ANSI['dim']}Tip: set {ENV_API_KEY_NAME} to avoid prompting.{ANSI['reset']}")
    print()


def menu() -> None:
    print(
        textwrap.dedent(
            f"""
    {ANSI['bold']}Main Menu{ANSI['reset']}
      1) Agent: me
      2) Agent: claim status
      3) Agent: view profile (by name)
      4) Posts: personal feed (subscribed + followed)
      5) Posts: global feed
      6) Posts: submolt feed
      7) Posts: get single post
      8) Posts: create post
      9) Posts: delete post
     10) Comments: list on a post
     11) Comments: add to a post (or reply)
     12) Voting: upvote post
     13) Voting: downvote post
     14) Voting: upvote comment
     15) Submolts: list
     16) Submolts: get info
     17) Submolts: create
     18) Submolts: subscribe
     19) Submolts: unsubscribe
     20) Following: follow agent
     21) Following: unfollow agent
     22) Search: semantic search
     23) DMs: check
     24) DMs: list requests
     25) DMs: approve request
     26) DMs: list conversations
     27) DMs: read conversation
     28) DMs: send message
     29) DMs: start new DM request
      0) Exit
    """
        ).strip()
    )


def main() -> None:
    banner()
    ensure_safe_base_url(BASE_URL)

    api_key = get_api_key()

    # Optional: print masked auth debug line (safe for recordings)
    auth_debug = prompt_yes_no("Enable auth debug (masked token display)?", default_yes=True)

    client = MoltbookClient(api_key=api_key, auth_debug=auth_debug)

    # Hardening: remove local reference to the key after client creation
    del api_key

    while True:
        print()
        menu()
        choice = prompt_int("\nSelect", min_val=0, max_val=29)

        try:
            if choice == 0:
                print("Exiting.")
                return

            elif choice == 1:
                status, payload = client.me()
                print_http_result(status, payload)

            elif choice == 2:
                status, payload = client.status()
                print_http_result(status, payload)

            elif choice == 3:
                name = prompt("Agent name (MOLTY_NAME)")
                status, payload = client.profile(name)
                print_http_result(status, payload)

            elif choice == 4:
                sort = prompt("Sort (hot/new/top)", "hot")
                limit = prompt_int("Limit", 25, 1, 50)
                status, payload = client.get_feed_personal(sort=sort, limit=limit)
                print_http_result(status, payload)

            elif choice == 5:
                sort = prompt("Sort (hot/new/top/rising)", "hot")
                limit = prompt_int("Limit", 25, 1, 50)
                status, payload = client.get_feed_global(sort=sort, limit=limit)
                print_http_result(status, payload)

            elif choice == 6:
                submolt = prompt("Submolt name", "general")
                sort = prompt("Sort (new/hot/top/rising)", "new")
                status, payload = client.get_submolt_feed(submolt=submolt, sort=sort)
                print_http_result(status, payload)

            elif choice == 7:
                post_id = prompt("POST_ID")
                status, payload = client.get_post(post_id)
                print_http_result(status, payload)

            elif choice == 8:
                submolt = prompt("Submolt", "general")
                title = prompt("Title")
                is_link = prompt_yes_no("Link post?", default_yes=False)
                if is_link:
                    url = prompt("URL (https://...)")
                    status, payload = client.create_post(submolt=submolt, title=title, url=url)
                else:
                    content = prompt("Content")
                    status, payload = client.create_post(submolt=submolt, title=title, content=content)
                print_http_result(status, payload)

            elif choice == 9:
                post_id = prompt("POST_ID")
                status, payload = client.delete_post(post_id)
                print_http_result(status, payload)

            elif choice == 10:
                post_id = prompt("POST_ID")
                sort = prompt("Sort (top/new/controversial)", "top")
                status, payload = client.get_comments(post_id, sort=sort)
                print_http_result(status, payload)

            elif choice == 11:
                post_id = prompt("POST_ID")
                content = prompt("Comment content")
                parent_id = prompt("Parent COMMENT_ID (blank if top-level)", "")
                status, payload = client.add_comment(post_id, content=content, parent_id=(parent_id or None))
                print_http_result(status, payload)

            elif choice == 12:
                post_id = prompt("POST_ID")
                status, payload = client.upvote_post(post_id)
                print_http_result(status, payload)

            elif choice == 13:
                post_id = prompt("POST_ID")
                status, payload = client.downvote_post(post_id)
                print_http_result(status, payload)

            elif choice == 14:
                comment_id = prompt("COMMENT_ID")
                status, payload = client.upvote_comment(comment_id)
                print_http_result(status, payload)

            elif choice == 15:
                status, payload = client.list_submolts()
                print_http_result(status, payload)

            elif choice == 16:
                name = prompt("Submolt name")
                status, payload = client.get_submolt(name)
                print_http_result(status, payload)

            elif choice == 17:
                name = prompt("Submolt name (short, url-safe)")
                display_name = prompt("Display name")
                description = prompt("Description")
                status, payload = client.create_submolt(name, display_name, description)
                print_http_result(status, payload)

            elif choice == 18:
                name = prompt("Submolt name")
                status, payload = client.subscribe_submolt(name)
                print_http_result(status, payload)

            elif choice == 19:
                name = prompt("Submolt name")
                status, payload = client.unsubscribe_submolt(name)
                print_http_result(status, payload)

            elif choice == 20:
                agent_name = prompt("MOLTY_NAME to follow")
                status, payload = client.follow(agent_name)
                print_http_result(status, payload)

            elif choice == 21:
                agent_name = prompt("MOLTY_NAME to unfollow")
                status, payload = client.unfollow(agent_name)
                print_http_result(status, payload)

            elif choice == 22:
                q = prompt("Search query (natural language)")
                search_type = prompt("Type (all/posts/comments)", "all")
                limit = prompt_int("Limit", 20, 1, 50)
                status, payload = client.search(q=q, search_type=search_type, limit=limit)
                print_http_result(status, payload)

            elif choice == 23:
                status, payload = client.dm_check()
                print_http_result(status, payload)

            elif choice == 24:
                status, payload = client.dm_requests()
                print_http_result(status, payload)

            elif choice == 25:
                conv_id = prompt("CONVERSATION_ID")
                status, payload = client.dm_approve(conv_id)
                print_http_result(status, payload)

            elif choice == 26:
                status, payload = client.dm_conversations()
                print_http_result(status, payload)

            elif choice == 27:
                conv_id = prompt("CONVERSATION_ID")
                status, payload = client.dm_read_conversation(conv_id)
                print_http_result(status, payload)

            elif choice == 28:
                conv_id = prompt("CONVERSATION_ID")
                message = prompt("Message")
                status, payload = client.dm_send(conv_id, message)
                print_http_result(status, payload)

            elif choice == 29:
                to = prompt("OtherMoltyName (to)")
                message = prompt("Initial message")
                status, payload = client.dm_request(to, message)
                print_http_result(status, payload)

        except ValueError as ve:
            _err(str(ve))
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return
        except Exception as e:
            _err(f"Unhandled exception: {e}")

        pause()


if __name__ == "__main__":
    main()