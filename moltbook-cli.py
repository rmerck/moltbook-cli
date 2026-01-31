#!/usr/bin/env python3
import json
import os
import sys
import time
import getpass
import socket
import ssl
import uuid
import mimetypes
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union, Tuple

BASE_URL = "https://www.moltbook.com"
API_BASE = f"{BASE_URL}/api/v1"
USER_AGENT = "moltbook-cli/2.1"
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RETRIES_IDEMPOTENT = 2
RETRY_BACKOFF_SECONDS = 1.5

CREDENTIALS_PATH = os.path.expanduser("~/.config/moltbook/credentials.json")


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, details: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.details = details or {}


def _ensure_www(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "moltbook.com":
        raise ValueError("Refusing moltbook.com without www (redirect can strip Authorization). Use https://www.moltbook.com")
    if host and host != "www.moltbook.com":
        raise ValueError("Refusing to send credentials to a non-moltbook domain.")
    return url


def _truncate(s: str, max_len: int = 300) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _pretty_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)


def _print_section(title: str) -> None:
    print("\n" + "=" * 88)
    print(title)
    print("=" * 88)


def _print_json(obj: Any) -> None:
    print(_pretty_json(obj))


def _prompt_nonempty(prompt: str, max_len: int = 4096) -> str:
    while True:
        s = input(prompt).strip()
        if not s:
            print("Input required.")
            continue
        if len(s) > max_len:
            print(f"Too long (max {max_len} chars).")
            continue
        return s


def _prompt_optional(prompt: str, max_len: int = 4096) -> Optional[str]:
    s = input(prompt).strip()
    if not s:
        return None
    if len(s) > max_len:
        print(f"Too long (max {max_len} chars).")
        return None
    return s


def _prompt_int(prompt: str, min_v: int, max_v: int, default: Optional[int] = None) -> int:
    while True:
        s = input(prompt).strip()
        if not s and default is not None:
            return default
        try:
            v = int(s)
        except ValueError:
            print("Enter a valid integer.")
            continue
        if v < min_v or v > max_v:
            print(f"Enter a value between {min_v} and {max_v}.")
            continue
        return v


def _confirm(prompt: str) -> bool:
    s = input(prompt + " [y/N]: ").strip().lower()
    return s in ("y", "yes")


def _sanitize_key(raw: str) -> str:
    if raw is None:
        return ""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    s = "".join(ch for ch in s if not ch.isspace())
    return s


def _mask_key(key: str) -> str:
    if not key:
        return "<empty>"
    if len(key) <= 12:
        return key[:2] + "…" + key[-2:]
    return key[:8] + "…" + key[-4:]


def _safe_show_error(err: ApiError) -> None:
    print(f"\nERROR: {err}")
    if err.status == 401:
        print("Hint: invalid/expired API key, or Authorization header not accepted. Ensure https://www.moltbook.com (with www).")
    if err.status == 403:
        print("Hint: valid key but not authorized for this action (or not claimed). Check Agent status.")
    if err.status == 404:
        print("Hint: endpoint not found; API may have changed or feature isn't enabled for your key.")
    if err.status == 429:
        print("Hint: rate limited. Respect retry_after_* fields if present.")
    if isinstance(err.details, dict) and err.details:
        hint = err.details.get("hint")
        if isinstance(hint, str) and hint.strip():
            print(f"Server hint: {hint.strip()}")
        ra_s = err.details.get("retry_after_seconds")
        ra_m = err.details.get("retry_after_minutes")
        dr = err.details.get("daily_remaining")
        if ra_s is not None:
            print(f"Retry after: {ra_s} second(s)")
        if ra_m is not None:
            print(f"Retry after: {ra_m} minute(s)")
        if dr is not None:
            print(f"Daily remaining: {dr}")


def _load_saved_credentials() -> Optional[Dict[str, str]]:
    try:
        if not os.path.exists(CREDENTIALS_PATH):
            return None
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        api_key = _sanitize_key(str(data.get("api_key", "")).strip())
        agent_name = str(data.get("agent_name", "")).strip()
        if api_key:
            return {"api_key": api_key, "agent_name": agent_name}
        return None
    except Exception:
        return None


def _save_credentials(api_key: str, agent_name: str) -> None:
    os.makedirs(os.path.dirname(CREDENTIALS_PATH), exist_ok=True)
    payload = {"api_key": api_key, "agent_name": agent_name}
    raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

    tmp_path = CREDENTIALS_PATH + ".tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
    os.replace(tmp_path, CREDENTIALS_PATH)
    try:
        os.chmod(CREDENTIALS_PATH, 0o600)
    except Exception:
        pass


def _validate_agent_name(name: str) -> Optional[str]:
    name = name.strip()
    if not name:
        return "Name is required."
    if len(name) > 32:
        return "Name too long (max 32)."
    if not (name[0].isalpha() and all(ch.isalnum() or ch == "_" for ch in name)):
        return "Name must start with a letter and contain only letters, numbers, underscore."
    return None


def _validate_submolt_name(name: str) -> Optional[str]:
    name = name.strip()
    if not name:
        return "Submolt name is required."
    if len(name) > 32:
        return "Submolt name too long (max 32)."
    if not all(ch.isalnum() or ch in ("_", "-") for ch in name):
        return "Submolt name must be url-safe: letters, numbers, underscore, hyphen."
    return None


def _build_multipart_form(file_field: str, file_path: str, extra_fields: Optional[Dict[str, str]] = None) -> Tuple[bytes, str]:
    if not os.path.isfile(file_path):
        raise ValueError("File does not exist.")
    max_bytes = 2 * 1024 * 1024
    size = os.path.getsize(file_path)
    if size > max_bytes:
        raise ValueError(f"File too large ({size} bytes). Max {max_bytes} bytes.")

    boundary = f"----moltbook-{uuid.uuid4().hex}"
    ct = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    filename = os.path.basename(file_path)

    parts: list[bytes] = []

    if extra_fields:
        for k, v in extra_fields.items():
            parts.append(f"--{boundary}\r\n".encode("utf-8"))
            parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8"))
            parts.append(str(v).encode("utf-8"))
            parts.append(b"\r\n")

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8")
    )
    parts.append(f"Content-Type: {ct}\r\n\r\n".encode("utf-8"))
    parts.append(file_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


@dataclass
class MoltbookClient:
    api_key: Optional[str] = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    auth_debug: bool = False

    def _headers(self, extra: Optional[Dict[str, str]] = None, include_auth: bool = True) -> Dict[str, str]:
        h: Dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Connection": "close",
        }
        if include_auth:
            if not self.api_key:
                raise ValueError("API key is not set for this operation.")
            h["Authorization"] = f"Bearer {self.api_key}"
            if self.auth_debug:
                print(f"[auth-debug] Authorization: Bearer {_mask_key(self.api_key)}")
        if extra:
            h.update(extra)
        return h

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Union[str, int, float, bool]]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        raw_body: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        expected_json: bool = True,
        include_auth: bool = True,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path

        url = _ensure_www(API_BASE + path)
        if params:
            qp = {k: str(v) for k, v in params.items() if v is not None}
            if qp:
                url = url + "?" + urllib.parse.urlencode(qp, doseq=True)

        if json_body is not None and raw_body is not None:
            raise ValueError("Provide either json_body or raw_body, not both.")

        data = None
        headers = self._headers(extra_headers, include_auth=include_auth)

        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif raw_body is not None:
            data = raw_body

        req = urllib.request.Request(url=url, method=method.upper(), data=data, headers=headers)

        is_idempotent = method.upper() in ("GET", "HEAD", "OPTIONS")
        retries = MAX_RETRIES_IDEMPOTENT if is_idempotent else 0

        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    body = resp.read() or b""
                    if not expected_json:
                        return body
                    if not body:
                        return {}
                    try:
                        return json.loads(body.decode("utf-8"))
                    except json.JSONDecodeError:
                        raise ApiError("Server returned non-JSON response.", status=getattr(resp, "status", None))
            except urllib.error.HTTPError as e:
                status = getattr(e, "code", None)
                body = b""
                try:
                    body = e.read() or b""
                except Exception:
                    body = b""

                parsed = None
                if body:
                    try:
                        parsed = json.loads(body.decode("utf-8", errors="replace"))
                    except Exception:
                        parsed = None

                msg = f"HTTP {status}"
                if isinstance(parsed, dict):
                    if isinstance(parsed.get("error"), str):
                        msg += f": {parsed['error']}"
                    elif isinstance(parsed.get("message"), str):
                        msg += f": {parsed['message']}"
                else:
                    txt = body.decode("utf-8", errors="replace").strip()
                    if txt:
                        msg += f": {_truncate(txt, 200)}"

                raise ApiError(msg, status=status, details=parsed if isinstance(parsed, dict) else {})
            except (TimeoutError, socket.timeout) as e:
                last_exc = e
                if attempt < retries:
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise ApiError("Request timed out. Increase timeout or retry later.") from e
            except ssl.SSLError as e:
                raise ApiError(f"TLS/SSL error: {_truncate(str(e), 200)}") from e
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", None)
                if isinstance(reason, (TimeoutError, socket.timeout)):
                    last_exc = e
                    if attempt < retries:
                        time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                        continue
                    raise ApiError("Request timed out. Increase timeout or retry later.") from e
                raise ApiError(f"Network error: {_truncate(str(reason), 200)}") from e
            except Exception as e:
                last_exc = e
                raise ApiError(f"Unexpected error: {_truncate(str(e), 200)}") from e

        raise ApiError(f"Request failed: {_truncate(str(last_exc), 200)}")

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params, include_auth=True)

    def post(self, path: str, json_body: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("POST", path, json_body=json_body, include_auth=True)

    def delete(self, path: str, json_body: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("DELETE", path, json_body=json_body, include_auth=True)

    def patch(self, path: str, json_body: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("PATCH", path, json_body=json_body, include_auth=True)

    # Registration is unauthenticated
    def register_agent(self, name: str, description: str) -> Any:
        return self.request("POST", "/agents/register", json_body={"name": name, "description": description}, include_auth=False)

    def post_multipart(self, path: str, body: bytes, content_type: str) -> Any:
        return self.request(
            "POST",
            path,
            raw_body=body,
            extra_headers={"Content-Type": content_type},
            include_auth=True,
        )


def _bootstrap() -> MoltbookClient:
    client = MoltbookClient(api_key=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, auth_debug=False)

    saved = _load_saved_credentials()
    if saved and saved.get("api_key"):
        client.api_key = saved["api_key"]
        return client

    env_key = _sanitize_key(os.environ.get("MOLTBOOK_API_KEY", ""))
    if env_key:
        client.api_key = env_key
        return client

    _print_section("Bootstrap")
    print("No API key found in env or saved credentials.")
    print("1) Register a new agent (get an API key)")
    print("2) Enter an existing API key")
    print("0) Quit")
    c = _prompt_int("Select: ", 0, 2)
    if c == 0:
        raise SystemExit(0)
    if c == 2:
        client.api_key = _sanitize_key(getpass.getpass("Enter Moltbook API key (hidden): "))
        if not client.api_key:
            raise ValueError("API key required.")
        return client

    # c == 1: register
    _print_section("Register agent (no API key required)")
    name = _prompt_nonempty("Agent name: ", max_len=64)
    err = _validate_agent_name(name)
    if err:
        raise ValueError(f"Invalid name: {err}")
    desc = _prompt_nonempty("Description: ", max_len=300)
    data = client.register_agent(name=name, description=desc)
    _print_json(data)

    agent = data.get("agent") if isinstance(data, dict) else None
    if not isinstance(agent, dict):
        raise ApiError("Registration succeeded but response missing agent object.")
    new_key = _sanitize_key(str(agent.get("api_key", "")).strip())
    if not new_key:
        raise ApiError("Registration response missing api_key.")
    claim_url = str(agent.get("claim_url", "")).strip()
    verification_code = str(agent.get("verification_code", "")).strip()

    print("\nIMPORTANT: Save your new API key and claim URL now.")
    print(f"API key (masked): {_mask_key(new_key)}")
    if claim_url:
        print(f"Claim URL: {claim_url}")
    if verification_code:
        print(f"Verification code: {verification_code}")

    if _confirm("Save credentials to ~/.config/moltbook/credentials.json (0600)?"):
        _save_credentials(new_key, name)
        print("Saved.")
    client.api_key = new_key
    return client


def menu() -> None:
    client = _bootstrap()

    while True:
        _print_section("Moltbook CLI")
        print("1) Register agent (creates new API key + claim URL)")
        print("2) Agent status")
        print("3) My profile (agents/me)")
        print("4) View agent profile (agents/profile)")
        print("5) Update my profile (PATCH agents/me)")
        print("6) Upload my avatar (POST agents/me/avatar)")
        print("7) Remove my avatar (DELETE agents/me/avatar)")
        print("8) Check DMs (quick)")
        print("9) List DM requests (pending)")
        print("10) Approve a DM request")
        print("11) Reject a DM request (optional block)")
        print("12) List DM conversations")
        print("13) Read a DM conversation")
        print("14) Send DM message")
        print("15) Send DM request")
        print("16) Feed (personalized)")
        print("17) Posts (global)")
        print("18) View post")
        print("19) Create post")
        print("20) Delete post")
        print("21) List comments on post")
        print("22) Comment on a post (or reply)")
        print("23) Upvote post")
        print("24) Downvote post")
        print("25) Upvote comment")
        print("26) Pin post")
        print("27) Unpin post")
        print("28) Search (semantic)")
        print("29) List submolts")
        print("30) View submolt")
        print("31) Create submolt")
        print("32) Subscribe submolt")
        print("33) Unsubscribe submolt")
        print("34) Update submolt settings (PATCH)")
        print("35) Upload submolt avatar/banner")
        print("36) Add submolt moderator")
        print("37) Remove submolt moderator")
        print("38) List submolt moderators")
        print("39) Follow agent")
        print("40) Unfollow agent")
        print("41) Set timeout")
        print("42) Toggle auth debug (masked)")
        print("43) Switch API key")
        print("0) Quit")

        choice = _prompt_int("\nSelect: ", 0, 43)

        try:
            if choice == 0:
                return

            if choice == 1:
                _print_section("Register agent (no API key required)")
                name = _prompt_nonempty("Agent name: ", max_len=64)
                err = _validate_agent_name(name)
                if err:
                    print(f"Invalid name: {err}")
                else:
                    desc = _prompt_nonempty("Description: ", max_len=300)
                    data = client.register_agent(name=name, description=desc)
                    _print_json(data)

                    agent = data.get("agent") if isinstance(data, dict) else None
                    if isinstance(agent, dict):
                        new_key = _sanitize_key(str(agent.get("api_key", "")).strip())
                        claim_url = str(agent.get("claim_url", "")).strip()
                        verification_code = str(agent.get("verification_code", "")).strip()

                        if new_key:
                            print("\nIMPORTANT: Save your new API key and claim URL now.")
                            print(f"API key (masked): {_mask_key(new_key)}")
                            if claim_url:
                                print(f"Claim URL: {claim_url}")
                            if verification_code:
                                print(f"Verification code: {verification_code}")

                            if _confirm("Save credentials to ~/.config/moltbook/credentials.json (0600)?"):
                                _save_credentials(new_key, name)
                                print("Saved.")
                            if _confirm("Use this new API key for the rest of this session?"):
                                client.api_key = new_key
                                print("Active API key updated.")

            elif choice == 2:
                _print_section("Agent status")
                data = client.get("/agents/status")
                _print_json(data)

            elif choice == 3:
                _print_section("My profile")
                data = client.get("/agents/me")
                _print_json(data)

            elif choice == 4:
                name = _prompt_nonempty("Agent name (MOLTY_NAME): ", max_len=64)
                _print_section(f"Agent profile: {name}")
                data = client.get("/agents/profile", params={"name": name})
                _print_json(data)

            elif choice == 5:
                _print_section("Update my profile (PATCH)")
                desc = _prompt_optional("New description (blank to skip): ", max_len=300)
                meta_raw = _prompt_optional("Metadata JSON (blank to skip): ", max_len=4000)
                payload: Dict[str, Any] = {}
                if desc:
                    payload["description"] = desc
                if meta_raw:
                    try:
                        meta = json.loads(meta_raw)
                        payload["metadata"] = meta
                    except Exception:
                        print("Invalid JSON for metadata.")
                if not payload:
                    print("Nothing to update.")
                else:
                    data = client.patch("/agents/me", json_body=payload)
                    _print_json(data)

            elif choice == 6:
                _print_section("Upload my avatar")
                path = _prompt_nonempty("Image path: ", max_len=1024)
                body, ct = _build_multipart_form("file", path, extra_fields=None)
                data = client.post_multipart("/agents/me/avatar", body, ct)
                _print_json(data)

            elif choice == 7:
                _print_section("Remove my avatar")
                data = client.delete("/agents/me/avatar")
                _print_json(data)

            elif choice == 8:
                _print_section("DM check")
                data = client.get("/agents/dm/check")
                _print_json(data)

            elif choice == 9:
                _print_section("DM requests (pending)")
                data = client.get("/agents/dm/requests")
                _print_json(data)

            elif choice == 10:
                conv_id = _prompt_nonempty("Request conversation ID to approve: ", max_len=200)
                _print_section("Approve request")
                data = client.post(f"/agents/dm/requests/{urllib.parse.quote(conv_id, safe='')}/approve")
                _print_json(data)

            elif choice == 11:
                conv_id = _prompt_nonempty("Request conversation ID to reject: ", max_len=200)
                block = _confirm("Also block future requests from this agent?")
                payload = {"block": True} if block else None
                _print_section("Reject request")
                data = client.post(f"/agents/dm/requests/{urllib.parse.quote(conv_id, safe='')}/reject", json_body=payload)
                _print_json(data)

            elif choice == 12:
                _print_section("DM conversations")
                data = client.get("/agents/dm/conversations")
                _print_json(data)

            elif choice == 13:
                conv_id = _prompt_nonempty("Conversation ID: ", max_len=200)
                _print_section(f"DM conversation: {conv_id}")
                data = client.get(f"/agents/dm/conversations/{urllib.parse.quote(conv_id, safe='')}")
                _print_json(data)

            elif choice == 14:
                conv_id = _prompt_nonempty("Conversation ID: ", max_len=200)
                msg = _prompt_nonempty("Message: ", max_len=1000)
                needs_human = _confirm("Flag needs_human_input")
                payload: Dict[str, Any] = {"message": msg}
                if needs_human:
                    payload["needs_human_input"] = True
                _print_section("Send DM message")
                data = client.post(f"/agents/dm/conversations/{urllib.parse.quote(conv_id, safe='')}/send", json_body=payload)
                _print_json(data)

            elif choice == 15:
                to = _prompt_optional("To (bot name) [blank to use to_owner]: ", max_len=200)
                to_owner = None
                if not to:
                    to_owner = _prompt_nonempty("To owner X handle (with or without @): ", max_len=200)
                    if to_owner.startswith("@"):
                        to_owner = to_owner[1:]
                msg = _prompt_nonempty("Request message (10-1000 chars): ", max_len=1000)
                if len(msg) < 10:
                    print("Message too short (min 10 chars).")
                else:
                    payload: Dict[str, Any] = {"message": msg}
                    if to:
                        payload["to"] = to
                    else:
                        payload["to_owner"] = to_owner
                    _print_section("Send DM request")
                    data = client.post("/agents/dm/request", json_body=payload)
                    _print_json(data)

            elif choice == 16:
                sort = _prompt_optional("Sort [hot/new/top] (default new): ", max_len=10) or "new"
                if sort not in ("hot", "new", "top"):
                    print("Invalid sort. Using 'new'.")
                    sort = "new"
                limit = _prompt_int("Limit (1-50, default 15): ", 1, 50, default=15)
                _print_section("Personalized feed")
                data = client.get("/feed", params={"sort": sort, "limit": limit})
                _print_json(data)

            elif choice == 17:
                sort = _prompt_optional("Sort [hot/new/top/rising] (default new): ", max_len=10) or "new"
                if sort not in ("hot", "new", "top", "rising"):
                    print("Invalid sort. Using 'new'.")
                    sort = "new"
                limit = _prompt_int("Limit (1-50, default 15): ", 1, 50, default=15)
                submolt = _prompt_optional("Submolt (optional): ", max_len=64)
                params: Dict[str, Any] = {"sort": sort, "limit": limit}
                if submolt:
                    params["submolt"] = submolt
                _print_section("Posts")
                data = client.get("/posts", params=params)
                _print_json(data)

            elif choice == 18:
                post_id = _prompt_nonempty("Post ID: ", max_len=200)
                _print_section(f"Post: {post_id}")
                data = client.get(f"/posts/{urllib.parse.quote(post_id, safe='')}")
                _print_json(data)

            elif choice == 19:
                submolt = _prompt_nonempty("Submolt (e.g., general): ", max_len=64)
                title = _prompt_nonempty("Title: ", max_len=200)
                content = _prompt_optional("Content (optional if URL post): ", max_len=20000)
                url = _prompt_optional("URL (optional for link post): ", max_len=2000)
                if not content and not url:
                    print("Must provide either content or url.")
                else:
                    payload: Dict[str, Any] = {"submolt": submolt, "title": title}
                    if content:
                        payload["content"] = content
                    if url:
                        payload["url"] = url
                    _print_section("Create post")
                    data = client.post("/posts", json_body=payload)
                    _print_json(data)

            elif choice == 20:
                post_id = _prompt_nonempty("Post ID: ", max_len=200)
                _print_section("Delete post")
                data = client.delete(f"/posts/{urllib.parse.quote(post_id, safe='')}")
                _print_json(data)

            elif choice == 21:
                post_id = _prompt_nonempty("Post ID: ", max_len=200)
                sort = _prompt_optional("Sort [top/new/controversial] (default top): ", max_len=20) or "top"
                if sort not in ("top", "new", "controversial"):
                    print("Invalid sort. Using 'top'.")
                    sort = "top"
                _print_section("Comments")
                data = client.get(f"/posts/{urllib.parse.quote(post_id, safe='')}/comments", params={"sort": sort})
                _print_json(data)

            elif choice == 22:
                post_id = _prompt_nonempty("Post ID: ", max_len=200)
                content = _prompt_nonempty("Comment content: ", max_len=5000)
                parent_id = _prompt_optional("Parent comment ID (optional): ", max_len=200)
                payload: Dict[str, Any] = {"content": content}
                if parent_id:
                    payload["parent_id"] = parent_id
                _print_section("Create comment")
                data = client.post(f"/posts/{urllib.parse.quote(post_id, safe='')}/comments", json_body=payload)
                _print_json(data)

            elif choice == 23:
                post_id = _prompt_nonempty("Post ID: ", max_len=200)
                _print_section("Upvote post")
                data = client.post(f"/posts/{urllib.parse.quote(post_id, safe='')}/upvote")
                _print_json(data)

            elif choice == 24:
                post_id = _prompt_nonempty("Post ID: ", max_len=200)
                _print_section("Downvote post")
                data = client.post(f"/posts/{urllib.parse.quote(post_id, safe='')}/downvote")
                _print_json(data)

            elif choice == 25:
                comment_id = _prompt_nonempty("Comment ID: ", max_len=200)
                _print_section("Upvote comment")
                data = client.post(f"/comments/{urllib.parse.quote(comment_id, safe='')}/upvote")
                _print_json(data)

            elif choice == 26:
                post_id = _prompt_nonempty("Post ID: ", max_len=200)
                _print_section("Pin post")
                data = client.post(f"/posts/{urllib.parse.quote(post_id, safe='')}/pin")
                _print_json(data)

            elif choice == 27:
                post_id = _prompt_nonempty("Post ID: ", max_len=200)
                _print_section("Unpin post")
                data = client.delete(f"/posts/{urllib.parse.quote(post_id, safe='')}/pin")
                _print_json(data)

            elif choice == 28:
                q = _prompt_nonempty("Search query (max 500 chars): ", max_len=500)
                t = _prompt_optional("Type [posts/comments/all] (default all): ", max_len=10) or "all"
                if t not in ("posts", "comments", "all"):
                    print("Invalid type. Using 'all'.")
                    t = "all"
                limit = _prompt_int("Limit (1-50, default 20): ", 1, 50, default=20)
                _print_section("Search results")
                data = client.get("/search", params={"q": q, "type": t, "limit": limit})
                _print_json(data)

            elif choice == 29:
                _print_section("Submolts")
                data = client.get("/submolts")
                _print_json(data)

            elif choice == 30:
                name = _prompt_nonempty("Submolt name: ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    _print_section(f"Submolt: {name}")
                    data = client.get(f"/submolts/{urllib.parse.quote(name, safe='')}")
                    _print_json(data)

            elif choice == 31:
                name = _prompt_nonempty("Submolt name (url-safe): ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    display_name = _prompt_nonempty("Display name: ", max_len=64)
                    description = _prompt_nonempty("Description: ", max_len=300)
                    _print_section("Create submolt")
                    data = client.post("/submolts", json_body={"name": name, "display_name": display_name, "description": description})
                    _print_json(data)

            elif choice == 32:
                name = _prompt_nonempty("Submolt name: ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    _print_section("Subscribe")
                    data = client.post(f"/submolts/{urllib.parse.quote(name, safe='')}/subscribe")
                    _print_json(data)

            elif choice == 33:
                name = _prompt_nonempty("Submolt name: ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    _print_section("Unsubscribe")
                    data = client.delete(f"/submolts/{urllib.parse.quote(name, safe='')}/subscribe")
                    _print_json(data)

            elif choice == 34:
                name = _prompt_nonempty("Submolt name: ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    desc = _prompt_optional("New description (blank to skip): ", max_len=300)
                    banner_color = _prompt_optional("Banner color (e.g. #1a1a2e) blank to skip: ", max_len=16)
                    theme_color = _prompt_optional("Theme color (e.g. #ff4500) blank to skip: ", max_len=16)
                    payload: Dict[str, Any] = {}
                    if desc:
                        payload["description"] = desc
                    if banner_color:
                        payload["banner_color"] = banner_color
                    if theme_color:
                        payload["theme_color"] = theme_color
                    if not payload:
                        print("Nothing to update.")
                    else:
                        _print_section("Update submolt settings")
                        data = client.patch(f"/submolts/{urllib.parse.quote(name, safe='')}/settings", json_body=payload)
                        _print_json(data)

            elif choice == 35:
                name = _prompt_nonempty("Submolt name: ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    t = _prompt_nonempty("Upload type [avatar/banner]: ", max_len=10).lower()
                    if t not in ("avatar", "banner"):
                        print("Invalid type.")
                    else:
                        path = _prompt_nonempty("Image path: ", max_len=1024)
                        body, ct = _build_multipart_form("file", path, extra_fields={"type": t})
                        _print_section(f"Upload submolt {t}")
                        data = client.post_multipart(f"/submolts/{urllib.parse.quote(name, safe='')}/settings", body, ct)
                        _print_json(data)

            elif choice == 36:
                name = _prompt_nonempty("Submolt name: ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    agent = _prompt_nonempty("Agent name to add: ", max_len=64)
                    role = _prompt_optional("Role (default moderator): ", max_len=16) or "moderator"
                    if role not in ("moderator", "owner"):
                        print("Invalid role. Using 'moderator'.")
                        role = "moderator"
                    _print_section("Add moderator")
                    data = client.post(f"/submolts/{urllib.parse.quote(name, safe='')}/moderators", json_body={"agent_name": agent, "role": role})
                    _print_json(data)

            elif choice == 37:
                name = _prompt_nonempty("Submolt name: ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    agent = _prompt_nonempty("Agent name to remove: ", max_len=64)
                    _print_section("Remove moderator")
                    data = client.delete(f"/submolts/{urllib.parse.quote(name, safe='')}/moderators", json_body={"agent_name": agent})
                    _print_json(data)

            elif choice == 38:
                name = _prompt_nonempty("Submolt name: ", max_len=64)
                err = _validate_submolt_name(name)
                if err:
                    print(f"Invalid submolt name: {err}")
                else:
                    _print_section("List moderators")
                    data = client.get(f"/submolts/{urllib.parse.quote(name, safe='')}/moderators")
                    _print_json(data)

            elif choice == 39:
                agent = _prompt_nonempty("Agent name to follow: ", max_len=64)
                _print_section("Follow agent")
                data = client.post(f"/agents/{urllib.parse.quote(agent, safe='')}/follow")
                _print_json(data)

            elif choice == 40:
                agent = _prompt_nonempty("Agent name to unfollow: ", max_len=64)
                _print_section("Unfollow agent")
                data = client.delete(f"/agents/{urllib.parse.quote(agent, safe='')}/follow")
                _print_json(data)

            elif choice == 41:
                v = _prompt_int("Timeout seconds (5-180): ", 5, 180, default=DEFAULT_TIMEOUT_SECONDS)
                client.timeout_seconds = v
                _print_section("Timeout updated")
                print(f"Timeout set to {v} seconds.")

            elif choice == 42:
                client.auth_debug = not client.auth_debug
                _print_section("Auth debug toggled")
                print(f"Auth debug is now: {'ON' if client.auth_debug else 'OFF'}")

            elif choice == 43:
                _print_section("Switch API key")
                print("1) Use saved credentials if present")
                print("2) Use env var MOLTBOOK_API_KEY if present")
                print("3) Enter API key now (hidden)")
                print("0) Cancel")
                c = _prompt_int("Select: ", 0, 3)
                if c == 0:
                    pass
                elif c == 1:
                    saved = _load_saved_credentials()
                    if not saved or not saved.get("api_key"):
                        print("No saved credentials found.")
                    else:
                        client.api_key = saved["api_key"]
                        print("Active API key updated (from saved credentials).")
                elif c == 2:
                    env_key = _sanitize_key(os.environ.get("MOLTBOOK_API_KEY", ""))
                    if not env_key:
                        print("No env key found.")
                    else:
                        client.api_key = env_key
                        print("Active API key updated (from env).")
                else:
                    k = _sanitize_key(getpass.getpass("Enter Moltbook API key (hidden): "))
                    if not k:
                        print("API key required.")
                    else:
                        client.api_key = k
                        print("Active API key updated.")
                        if _confirm("Save to ~/.config/moltbook/credentials.json (0600)?"):
                            agent_name = _prompt_optional("Agent name to save (optional): ", max_len=64) or ""
                            _save_credentials(client.api_key, agent_name)
                            print("Saved.")

        except ApiError as e:
            _safe_show_error(e)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return
        except Exception as e:
            print(f"\nERROR: {_truncate(str(e), 300)}")

        input("\nPress Enter to continue...")


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {_truncate(str(e), 300)}")
        sys.exit(1)
