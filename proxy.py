#!/usr/bin/env python3
# ============================================================
#   ██████╗  █████╗ ████████╗██╗   ██╗
#  ██╔════╝ ██╔══██╗╚══██╔══╝╚██╗ ██╔╝
#  ██║  ███╗███████║   ██║    ╚████╔╝
#  ██║   ██║██╔══██║   ██║     ╚██╔╝
#  ╚██████╔╝██║  ██║   ██║      ██║
#   ╚═════╝ ╚═╝  ╚═╝   ╚═╝      ╚═╝
#
#  Gaty — DeepSeek Web Bridge Proxy
#  Bridges Claude CLI (and any OpenAI/Anthropic client)
#  to your logged-in DeepSeek browser tab via WebSocket.
#
#  Usage:
#    python proxy.py
#    python proxy.py --host 127.0.0.1 --port 1337 --reset-threshold 150000
#
#  Set these env vars (or put them in ~/.claude/settings.json):
#    ANTHROPIC_API_KEY=nah
#    ANTHROPIC_BASE_URL=http://127.0.0.1:1337
#    ANTHROPIC_MODEL=deepseek-reasoner
# ============================================================

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Optional

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

# ── Gaty: server config ────────────────────────────────────────────────────────
GATY_HOST = "127.0.0.1"
GATY_PORT = 1337

# ── Gaty: chat reset trigger words ────────────────────────────────────────────
GATY_RESET_COMMANDS = {
    "/clear", "/reset", "/new", "/clearchat", "/resetchat", "/deletecurrentchat",
    "!clear", "!reset", "!new", "!clearchat", "!resetchat", "!deletecurrentchat",
    "?clear", "?reset", "?new", "?clearchat", "?resetchat", "?deletecurrentchat",
}

# ── Gaty: tuning knobs ────────────────────────────────────────────────────────
GATY_JOB_TIMEOUT       = 600     # seconds to wait for DeepSeek to reply
GATY_MAX_PROMPT_CHARS  = 24000   # truncate prompts longer than this
GATY_HISTORY_TAIL      = 12      # only keep last N conversation turns
GATY_ARG_CHUNK_SIZE    = 24      # stream tool-arg JSON in chunks of this size
GATY_AUTO_RESET_TOKENS = 150000  # auto-wipe chat when session tokens hit this (0=off)
GATY_MIN_JOB_INTERVAL  = 1.5     # minimum seconds between browser submissions

# ── Gaty: model list served to clients ────────────────────────────────────────
GATY_MODELS = [
    {"id": "deepseek-chat",     "object": "model", "created": 0, "owned_by": "gaty"},
    {"id": "deepseek-reasoner", "object": "model", "created": 0, "owned_by": "gaty"},
]

# ── Gaty: global WebSocket state ──────────────────────────────────────────────
gaty_active_ws: Optional[web.WebSocketResponse] = None
gaty_pending_jobs: dict[str, asyncio.Future]    = {}
_gaty_job_lock: Optional[asyncio.Lock]          = None


def gaty_job_lock() -> asyncio.Lock:
    # Lazy-init the lock so this module can be imported without a running event loop
    global _gaty_job_lock
    if _gaty_job_lock is None:
        _gaty_job_lock = asyncio.Lock()
    return _gaty_job_lock


# ── Gaty: tool-use system prompt injected when tools are present ───────────────
GATY_TOOL_PROTOCOL = """You are an AI assistant exposed through an OpenAI-compatible API with tool support.

You have access to the following tools:
{tool_list}

TOOL INSTRUCTIONS:
1. When you need to take an action, output a tool call using this EXACT XML format:
<tool_call>
<name>TOOL_NAME</name>
<arguments>{{"argument_name": "argument value"}}</arguments>
</tool_call>

2. <arguments> must be a valid JSON object.
3. Use ONLY tool names from the available tools list above.
4. When you do NOT need a tool, respond in normal conversational text / markdown.
   Format code inside markdown code fences (e.g. ```python ... ```).
5. NEVER use bash/echo/Write-Output just to speak to the user. Speak directly.
"""

# ── Gaty: Cline XML protocol injected when Cline system prompt is detected ─────
GATY_CLINE_PROTOCOL = """# Tool Use Formatting

You are an AI assistant integrated into Cline (a VS Code coding agent).
You MUST respond using ONLY the following XML tool format.

## attempt_completion
<attempt_completion>
<result>
Your final answer or task summary goes here.
</result>
</attempt_completion>

## ask_followup_question
<ask_followup_question>
<question>Your question goes here.</question>
</ask_followup_question>

Rules:
1. Every response MUST be exactly one tool call block.
2. Do not wrap the XML in markdown code fences.
3. Do not include any text outside the XML block.
"""

# Markers that indicate a Cline-style system prompt
GATY_CLINE_MARKERS = (
    "<attempt_completion>",
    "<ask_followup_question>",
    "# Tool Use",
    "TOOL USE",
)

# ── Gaty: CORS headers sent on every response ─────────────────────────────────
GATY_CORS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}

# ── Gaty: compiled regexes for tool-call parsing ──────────────────────────────
_GATY_TOOL_BLOCK_RE = re.compile(
    r"<tool_call>(.*?)(?:</tool_call>|</[|\uFF5C]{2}DSML[|\uFF5C]{2}\s*(?:calls|invoke|parameter)>|(?=<tool_call>)|$)",
    re.DOTALL | re.IGNORECASE,
)
_GATY_TAG_NAME_RE  = re.compile(r"<name>(.*?)</name>",       re.DOTALL | re.IGNORECASE)
_GATY_TAG_ARGS_RE  = re.compile(
    r"<arguments>(.*?)(?:</arguments>|</[|\uFF5C]{2}DSML[|\uFF5C]{2}\s*(?:parameter|invoke|calls)>|$)",
    re.DOTALL | re.IGNORECASE,
)
_GATY_JSON_OBJ_RE  = re.compile(r"\{.*\}", re.DOTALL)
_GATY_FENCE_RE     = re.compile(r"```[a-zA-Z0-9_-]*\s*|\s*```")
_GATY_DSML_INVOKE_RE = re.compile(
    r"<[|\uFF5C]{2}DSML[|\uFF5C]{2}\s+invoke\s+name=[\"']([^\"']+)[\"']\s*>(.*?)(?:</[|\uFF5C]{2}DSML[|\uFF5C]{2}\s+invoke>|$)",
    re.DOTALL | re.IGNORECASE,
)
_GATY_DSML_PARAM_RE = re.compile(
    r"<[|\uFF5C]{2}DSML[|\uFF5C]{2}\s+parameter\s+name=[\"']([^\"']+)[\"'](?:\s+[^>]*)?>(.*?)(?:</[|\uFF5C]{2}DSML[|\uFF5C]{2}\s+parameter>|$)",
    re.DOTALL | re.IGNORECASE,
)
_GATY_CLINE_ATTEMPT_RE = re.compile(
    r"<attempt_completion>\s*<result>(.*?)</result>\s*</attempt_completion>",
    re.DOTALL | re.IGNORECASE,
)
_GATY_CLINE_ASK_RE = re.compile(
    r"<ask_followup_question>\s*<question>(.*?)</question>\s*</ask_followup_question>",
    re.DOTALL | re.IGNORECASE,
)

# ── Gaty: last job timestamp (rate-limit guard) ────────────────────────────────
_gaty_last_job_time: float = 0.0


# ==============================================================================
#  Gaty WebSocket handler — browser tab connects here
# ==============================================================================
async def gaty_websocket_handler(request: web.Request) -> web.WebSocketResponse:
    global gaty_active_ws
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    gaty_active_ws = ws
    print("\033[95m[Gaty]\033[0m \033[92mDeepSeek browser tab connected! 💜\033[0m")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    print(f"\033[91m[Gaty]\033[0m Bad JSON from browser: {msg.data[:200]!r}")
                    continue
                # Route the response to the waiting future
                req_id = data.get("id")
                future = gaty_pending_jobs.get(req_id) if req_id else None
                if future and not future.done():
                    future.set_result(data)
            elif msg.type in (web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                break
    finally:
        if gaty_active_ws == ws:
            gaty_active_ws = None
        # Unblock any jobs that were waiting when the tab disconnected
        for fut in list(gaty_pending_jobs.values()):
            if not fut.done():
                fut.set_result({"error": "Gaty: browser tab disconnected mid-request."})
        print("\033[95m[Gaty]\033[0m \033[93mDeepSeek browser tab disconnected.\033[0m")

    return ws


# ==============================================================================
#  Gaty message helpers
# ==============================================================================
def gaty_flatten_message(message: dict) -> str:
    # Flatten OpenAI-style message content (string or list of blocks) into plain text
    parts = []
    content = message.get("content", "")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "text" in block:
                    parts.append(str(block["text"]))
    elif content:
        parts.append(str(content))

    # Also stringify any tool_calls so they appear in the prompt
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            fn   = tc.get("function", tc) if isinstance(tc, dict) else {}
            name = fn.get("name", "")
            args = fn.get("arguments", "")
            if isinstance(args, dict):
                args = json.dumps(args, ensure_ascii=False)
            if name:
                parts.append(f"<tool_call>\n<name>{name}</name>\n<arguments>{args}</arguments>\n</tool_call>")

    return "\n".join(p for p in parts if p)


def gaty_truncate_middle(text: str, limit: int) -> str:
    # Keep the start and end of a prompt, cut the middle if too long
    if len(text) <= limit:
        return text
    keep = limit // 2
    return text[:keep] + "\n\n[... Gaty truncated middle ...]\n\n" + text[-keep:]


def gaty_build_tool_list(tools: list[dict]) -> str:
    # Format the tool list for injection into the system prompt
    lines = []
    for tool in tools:
        fn   = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name", "unknown")
        desc = (fn.get("description") or "").strip().replace("\n", " ")
        params = fn.get("parameters") or {}
        lines.append(f"- {name}: {desc}")
        lines.append(f"  parameters: {json.dumps(params, ensure_ascii=False)}")
    return "\n".join(lines)


def gaty_detect_cline(messages: list[dict]) -> bool:
    # Check whether any system message contains Cline XML markers
    for message in messages:
        if message.get("role") == "system":
            text = gaty_flatten_message(message)
            if any(marker in text for marker in GATY_CLINE_MARKERS):
                return True
    return False


def gaty_build_prompt(
    messages: list[dict],
    tools: Optional[list[dict]],
    tool_choice: Any = None,
) -> tuple[str, str]:
    # Build the full prompt string sent to DeepSeek, and return the mode string
    use_tools = bool(tools) and tool_choice != "none"
    is_cline  = (not use_tools) and gaty_detect_cline(messages)

    if use_tools:
        mode = "native_tools"
    elif is_cline:
        mode = "cline_xml"
    else:
        mode = "text"

    system_parts: list[str] = []
    conversation: list[str] = []

    for message in messages:
        role = (message.get("role") or "user").lower()
        text = gaty_flatten_message(message)

        if role == "system":
            system_parts.append(text)
        elif role == "user":
            conversation.append(f"User:\n{text}")
        elif role == "assistant":
            if text:
                conversation.append(f"Assistant:\n{text}")
        elif role == "tool":
            conversation.append(f"Tool result:\n{text}")

    # Trim conversation history to last N turns
    if len(conversation) > GATY_HISTORY_TAIL:
        conversation = conversation[-GATY_HISTORY_TAIL:]

    sections: list[str] = []

    if mode == "native_tools":
        sections.append(GATY_TOOL_PROTOCOL.format(tool_list=gaty_build_tool_list(tools or [])))
    elif mode == "cline_xml":
        sections.append(GATY_CLINE_PROTOCOL)

    if system_parts:
        sections.append("# System Context\n" + "\n\n".join(system_parts))

    if conversation:
        sections.append("# Conversation\n" + "\n\n".join(conversation))

    prompt = "\n\n".join(sections).strip()
    prompt = gaty_truncate_middle(prompt, GATY_MAX_PROMPT_CHARS)
    return prompt, mode


# ==============================================================================
#  Gaty tool-call parser
# ==============================================================================
def gaty_strip_fences(text: str) -> str:
    return _GATY_FENCE_RE.sub("", text).strip()


def gaty_ensure_fenced(text: str) -> str:
    # Wrap bare code in markdown fences if it looks like code
    trimmed = text.strip()
    if "```" in trimmed or not trimmed:
        return text

    code_hints = (
        ("python",     ("with open(", "import ", "from ", "def ", "class ", "print(", "if __name__")),
        ("powershell", ("Get-", "Set-", "New-Item", "Remove-Item", "Write-Output", "Test-Path")),
        ("bash",       ("#!/bin/bash", "#!/bin/sh", "sudo ", "curl ", "npm ", "git ", "pip ", "docker ")),
        ("javascript", ("const ", "let ", "var ", "function(", "function ", "console.log(", "export ")),
    )

    first_line = trimmed.split("\n", 1)[0].strip()
    for lang, markers in code_hints:
        if any(first_line.startswith(m) for m in markers):
            return f"```{lang}\n{trimmed}\n```"

    lines = trimmed.split("\n")
    if len(lines) >= 2:
        if any(l.strip().startswith(("import ", "from ", "def ", "class ", "with open(", "with ")) for l in lines[:3]):
            return f"```python\n{trimmed}\n```"

    return text


def gaty_loads_lenient(raw: str) -> Optional[Any]:
    # Parse JSON tolerating trailing commas and unescaped newlines
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None

    def escape_nl(text: str) -> str:
        result: list[str] = []
        in_string = False
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\\" and in_string:
                result.append(c)
                i += 1
                if i < len(text):
                    result.append(text[i])
                    i += 1
                continue
            if c == '"':
                in_string = not in_string
                result.append(c)
            elif in_string and c == "\n":
                result.append("\\n")
            elif in_string and c == "\r":
                if i + 1 < len(text) and text[i + 1] == "\n":
                    i += 1
                result.append("\\n")
            else:
                result.append(c)
            i += 1
        return "".join(result)

    no_trail        = re.sub(r",\s*([}\]])", r"\1", candidate)
    escaped         = escape_nl(candidate)
    escaped_no_trail = re.sub(r",\s*([}\]])", r"\1", escaped)

    for attempt in (candidate, no_trail, escaped, escaped_no_trail):
        try:
            return json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def gaty_find_json(text: str) -> Optional[dict]:
    # Find the first valid JSON object anywhere in the text
    for match in _GATY_JSON_OBJ_RE.finditer(text):
        parsed = gaty_loads_lenient(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    return None


def gaty_validate_calls(calls: list[dict], tool_names: list[str]) -> list[dict]:
    # Filter out calls whose names don't match the declared tool list
    valid = []
    for call in calls:
        name = call["name"]
        if tool_names and name not in tool_names:
            tail = name.split(".")[-1]
            if tail in tool_names:
                name = tail
            else:
                continue
        valid.append({"name": name, "arguments": call["arguments"]})
    return valid


def gaty_parse_tool_calls(raw: str, tool_names: list[str]) -> list[dict]:
    # Extract tool calls from DeepSeek's raw output (XML or DSML format)
    if not raw:
        return []

    calls: list[dict] = []

    # Pass 1: <tool_call> XML blocks
    for block in _GATY_TOOL_BLOCK_RE.findall(raw):
        block = block.strip()
        if not block:
            continue
        name_m = _GATY_TAG_NAME_RE.search(block)
        args_m = _GATY_TAG_ARGS_RE.search(block)

        if name_m:
            name     = name_m.group(1).strip()
            args_raw = args_m.group(1).strip() if args_m else ""
            arguments = gaty_loads_lenient(args_raw) if args_raw else None
            if not isinstance(arguments, dict):
                arguments = gaty_find_json(block) or (gaty_find_json(args_raw) if args_raw else None)
            if not isinstance(arguments, dict):
                arguments = {"result": gaty_strip_fences(args_raw)} if args_raw else {}
            calls.append({"name": name, "arguments": arguments})
            continue

        payload = gaty_find_json(block)
        if isinstance(payload, dict):
            name = payload.get("name") or payload.get("tool") or payload.get("function")
            args = payload.get("arguments") or payload.get("parameters") or payload.get("args")
            if isinstance(args, str):
                args = gaty_loads_lenient(args)
            if name:
                calls.append({"name": str(name), "arguments": args if isinstance(args, dict) else {"result": args}})

    if calls:
        return gaty_validate_calls(calls, tool_names)

    # Pass 2: DeepSeek native DSML format
    for m in _GATY_DSML_INVOKE_RE.finditer(raw):
        name = m.group(1).strip()
        body = m.group(2)
        args: dict = {}
        for pm in _GATY_DSML_PARAM_RE.finditer(body):
            pname = pm.group(1).strip()
            pval  = pm.group(2).strip()
            if pval.lower()   == "true":  args[pname] = True
            elif pval.lower() == "false": args[pname] = False
            elif pval.lower() == "null":  args[pname] = None
            elif pval.isdigit():          args[pname] = int(pval)
            else:
                try:    args[pname] = json.loads(pval)
                except: args[pname] = pval
        calls.append({"name": name, "arguments": args})

    if calls:
        return gaty_validate_calls(calls, tool_names)

    # Pass 3: bare JSON object fallback
    payload = gaty_find_json(gaty_strip_fences(raw))
    if isinstance(payload, dict):
        name = payload.get("name") or payload.get("tool") or payload.get("function")
        args = payload.get("arguments") or payload.get("parameters") or payload.get("args")
        if isinstance(args, str):
            args = gaty_loads_lenient(args)
        if name:
            return gaty_validate_calls(
                [{"name": str(name), "arguments": args if isinstance(args, dict) else {"result": args}}],
                tool_names,
            )

    return []


def gaty_coerce_args(arguments: dict, tool_schema: Optional[dict]) -> dict:
    # Best-effort alignment of parsed args with the tool's declared JSON schema
    if not isinstance(arguments, dict):
        return {"result": str(arguments)}
    if not tool_schema:
        return arguments

    properties = tool_schema.get("properties") or {}
    required   = tool_schema.get("required") or []

    if all(key in arguments for key in required):
        return arguments

    if len(required) == 1:
        key = required[0]
        if key not in arguments and arguments:
            if len(arguments) == 1:
                arguments = {key: next(iter(arguments.values()))}
            else:
                for candidate in ("result", "text", "content", "response", "answer", "value"):
                    if candidate in arguments:
                        arguments = {key: arguments[candidate]}
                        break

    if properties and not tool_schema.get("additionalProperties", True):
        arguments = {k: v for k, v in arguments.items() if k in properties}

    return arguments


def gaty_normalize_cline_xml(raw: str) -> str:
    # Ensure Cline-mode output is properly wrapped in attempt_completion XML
    text = (raw or "").strip()
    if not text:
        return "<attempt_completion><result>(empty response)</result></attempt_completion>"

    m = _GATY_CLINE_ATTEMPT_RE.search(text)
    if m:
        inner = m.group(1).strip()
        return f"<attempt_completion>\n<result>\n{inner}\n</result>\n</attempt_completion>"

    m = _GATY_CLINE_ASK_RE.search(text)
    if m:
        inner = m.group(1).strip()
        return f"<ask_followup_question>\n<question>\n{inner}\n</question>\n</ask_followup_question>"

    text = gaty_strip_fences(text)
    if "<attempt_completion>" in text or "<ask_followup_question>" in text:
        return text

    return f"<attempt_completion>\n<result>\n{text}\n</result>\n</attempt_completion>"


# ==============================================================================
#  Gaty SSE / streaming helpers
# ==============================================================================
def gaty_make_chunk(
    completion_id: str,
    created: int,
    model: str,
    delta: dict,
    finish_reason: Optional[str] = None,
) -> dict:
    # Build an OpenAI-format SSE chunk dict
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def gaty_usage(prompt: str, completion: str, accumulated: int = 0, initial: int = 0) -> dict:
    # Estimate token usage (or use real counts from DeepSeek if available)
    if accumulated > 0:
        total      = accumulated
        comp_toks  = max(1, accumulated - initial) if initial > 0 else max(1, len(completion) // 4)
        prompt_toks = max(1, total - comp_toks)
        return {"prompt_tokens": prompt_toks, "completion_tokens": comp_toks, "total_tokens": total}
    return {
        "prompt_tokens":     max(1, len(prompt) // 4),
        "completion_tokens": max(1, len(completion) // 4),
        "total_tokens":      max(1, (len(prompt) + len(completion)) // 4),
    }


async def gaty_sse_write(response: web.StreamResponse, payload: dict) -> None:
    # Write a single SSE data frame — silently drop if the client disconnected
    try:
        data = json.dumps(payload, ensure_ascii=False)
        await response.write(f"data: {data}\n\n".encode("utf-8"))
    except (ClientConnectionResetError, ConnectionResetError):
        pass  # Client closed the connection mid-stream
    except Exception as exc:
        if "closing transport" in str(exc):
            pass
        else:
            raise


def gaty_iter_chunks(text: str, size: int = GATY_ARG_CHUNK_SIZE):
    # Yield text in fixed-size chunks for streaming
    for i in range(0, len(text), size):
        yield text[i : i + size]


# ==============================================================================
#  Gaty browser job runner
# ==============================================================================
async def gaty_run_job(prompt: str, thinking: bool) -> dict:
    # Send a prompt to the DeepSeek browser tab and wait for the response
    global _gaty_last_job_time
    async with gaty_job_lock():
        if not gaty_active_ws or gaty_active_ws.closed:
            return {"error": "Gaty: DeepSeek browser tab is not connected."}

        # Rate-limit guard — don't spam the browser
        elapsed = time.time() - _gaty_last_job_time
        if elapsed < GATY_MIN_JOB_INTERVAL:
            await asyncio.sleep(GATY_MIN_JOB_INTERVAL - elapsed)

        req_id = f"gaty-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        loop   = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        gaty_pending_jobs[req_id] = future

        try:
            await gaty_active_ws.send_str(json.dumps({
                "id":             req_id,
                "prompt":         prompt,
                "thinkingEnabled": bool(thinking),
            }))
            res = await asyncio.wait_for(future, timeout=GATY_JOB_TIMEOUT)
            _gaty_last_job_time = time.time()
            return res
        except asyncio.TimeoutError:
            _gaty_last_job_time = time.time()
            return {"error": "Gaty: DeepSeek generation timed out."}
        finally:
            gaty_pending_jobs.pop(req_id, None)


async def gaty_trigger_reset() -> None:
    # Fire-and-forget: tell the browser tab to delete the current chat
    await asyncio.sleep(0.5)
    async with gaty_job_lock():
        if gaty_active_ws and not gaty_active_ws.closed:
            try:
                await gaty_active_ws.send_str(json.dumps({
                    "action": "delete_chat",
                    "id":     f"gaty-reset-{int(time.time())}",
                }))
            except Exception:
                pass


# ==============================================================================
#  Gaty: error response helper
# ==============================================================================
def gaty_error(message: str, status: int, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": None}},
        status=status,
        headers=GATY_CORS,
    )


# ==============================================================================
#  Gaty: /v1/models  — model list endpoint
# ==============================================================================
async def gaty_handle_models(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=GATY_CORS)
    return web.json_response({"object": "list", "data": GATY_MODELS}, headers=GATY_CORS)


# ==============================================================================
#  Gaty: /health  — quick health check
# ==============================================================================
async def gaty_handle_health(request: web.Request) -> web.Response:
    return web.json_response({
        "status":           "gaty-ok 💜",
        "browser_connected": bool(gaty_active_ws and not gaty_active_ws.closed),
        "pending_jobs":      len(gaty_pending_jobs),
    }, headers=GATY_CORS)


# ==============================================================================
#  Gaty: Cline detection helper
# ==============================================================================
def gaty_is_cline(request: web.Request, messages: Optional[list[dict]] = None) -> bool:
    # Cline's SSE parser can't handle tool_calls chunks, so we downgrade to non-streaming
    ua = request.headers.get("User-Agent", "")
    if "cline" in ua.lower():
        return True
    if request.headers.get("X-Cline-Version"):
        return True
    if messages and gaty_detect_cline(messages):
        return True
    return False


# ==============================================================================
#  Gaty: core response builder (shared between OpenAI and Anthropic handlers)
# ==============================================================================
async def gaty_core_complete(
    model: str,
    messages: list[dict],
    tools: Optional[list[dict]],
    tool_choice: Any,
    stream: bool,
    stream_include_usage: bool,
    request: web.Request,
) -> web.Response:
    # Determine if this is a Cline client and downgrade streaming if so
    if gaty_is_cline(request, messages) and stream:
        stream = False
        print("\033[95m[Gaty]\033[0m Cline detected — downgrading to non-streaming")

    is_reasoner = "reasoner" in model.lower() or "r1" in model.lower()

    # Collect tool names and schemas for later validation
    tool_names:   list[str]        = []
    tool_schemas: dict[str, dict]  = {}
    if isinstance(tools, list):
        for tool in tools:
            fn   = tool.get("function", tool) if isinstance(tool, dict) else {}
            name = fn.get("name")
            if name:
                tool_names.append(name)
                tool_schemas[name] = fn.get("parameters") or {}

    prompt, mode = gaty_build_prompt(messages, tools, tool_choice)

    print(
        f"\033[95m[Gaty]\033[0m \033[96m[Request]\033[0m "
        f"model={model} stream={stream} mode={mode} "
        f"tools={len(tool_names)} reasoner={is_reasoner}"
    )

    # Check for reset commands
    last_user = ""
    for m in reversed(messages):
        if (m.get("role") or "").lower() == "user":
            last_user = gaty_flatten_message(m).strip()
            break

    if last_user.lower() in GATY_RESET_COMMANDS:
        print(f"\033[95m[Gaty]\033[0m Reset command: {last_user}")
        async with gaty_job_lock():
            if gaty_active_ws and not gaty_active_ws.closed:
                try:
                    await gaty_active_ws.send_str(json.dumps({
                        "action": "delete_chat",
                        "id":     f"gaty-cmd-{int(time.time())}",
                    }))
                except Exception:
                    pass
        reply = "Gaty: chat session cleared. Fresh start! 💜"
        cid   = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        now   = int(time.time())
        if not stream:
            return web.json_response({
                "id": cid, "object": "chat.completion", "created": now, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": gaty_usage(last_user, reply),
            }, headers=GATY_CORS)
        resp = web.StreamResponse(status=200, headers={**GATY_CORS, "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache, no-transform", "Connection": "keep-alive"})
        await resp.prepare(request)
        await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, {"role": "assistant", "content": reply}))
        await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, {}, "stop"))
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    # Send to DeepSeek
    result = await gaty_run_job(prompt, is_reasoner)
    if "error" in result:
        return gaty_error(result["error"], 502, "server_error")

    raw_text  = (result.get("text")      or "").strip()
    reasoning = (result.get("reasoning") or "").strip()

    print(f"\033[95m[Gaty]\033[0m \033[93m[Raw]\033[0m {repr(raw_text[:300])}")

    cid   = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    now   = int(time.time())
    calls: list[dict] = []
    content: str      = ""

    if mode == "native_tools":
        parsed = gaty_parse_tool_calls(raw_text, tool_names)
        if parsed:
            for idx, call in enumerate(parsed):
                args = gaty_coerce_args(call["arguments"], tool_schemas.get(call["name"]))
                calls.append({"id": f"call_{uuid.uuid4().hex[:24]}", "index": idx, "name": call["name"], "arguments": args})
        elif "attempt_completion" in tool_names:
            calls.append({"id": f"call_{uuid.uuid4().hex[:24]}", "index": 0, "name": "attempt_completion",
                          "arguments": {"result": gaty_ensure_fenced(raw_text) or "(empty)"}})
        else:
            content = gaty_ensure_fenced(raw_text)
    elif mode == "cline_xml":
        content = gaty_normalize_cline_xml(raw_text)
    else:
        content = gaty_ensure_fenced(raw_text)

    accumulated = int(result.get("accumulatedTokens") or 0)
    initial     = int(result.get("initialTokens")     or 0)
    usage       = gaty_usage(prompt, raw_text, accumulated, initial)

    print(f"\033[95m[Gaty]\033[0m \033[92m[Success]\033[0m mode={mode} text={len(raw_text)} calls={len(calls)} content={len(content)}")

    # Auto-reset when session tokens overflow
    if GATY_AUTO_RESET_TOKENS > 0 and accumulated >= GATY_AUTO_RESET_TOKENS:
        print(f"\033[95m[Gaty]\033[0m Auto-reset: {accumulated} >= {GATY_AUTO_RESET_TOKENS} tokens")
        asyncio.create_task(gaty_trigger_reset())

    if not stream:
        if calls:
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                for c in calls
            ]}
            finish = "tool_calls"
        else:
            msg = {"role": "assistant", "content": content}
            if reasoning:
                msg["reasoning_content"] = reasoning
            finish = "stop"
        return web.json_response({
            "id": cid, "object": "chat.completion", "created": now, "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": usage,
        }, headers=GATY_CORS)

    # Streaming response
    resp = web.StreamResponse(status=200, reason="OK", headers={
        **GATY_CORS,
        "Content-Type":    "text/event-stream; charset=utf-8",
        "Cache-Control":   "no-cache, no-transform",
        "Connection":      "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    if calls:
        # Stream tool call chunks
        await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, {
            "role": "assistant", "content": None,
            "tool_calls": [{"index": c["index"], "id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": ""}} for c in calls],
        }))
        for call in calls:
            serialized = json.dumps(call["arguments"], ensure_ascii=False)
            for frag in gaty_iter_chunks(serialized):
                await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, {"tool_calls": [{"index": call["index"], "function": {"arguments": frag}}]}))
        await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, {}, "tool_calls"))
    else:
        first = True
        # Stream reasoning first (DeepSeek-R1 thinking tokens)
        if reasoning:
            for frag in gaty_iter_chunks(reasoning, size=64):
                delta = {"reasoning_content": frag}
                if first:
                    delta["role"] = "assistant"
                    first = False
                await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, delta))
        # Stream main content
        frag_count = 0
        for frag in gaty_iter_chunks(content, size=64):
            frag_count += 1
            delta = {"content": frag}
            if first:
                delta["role"] = "assistant"
                first = False
            await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, delta))
        if first:
            await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, {"role": "assistant", "content": ""}))
        await gaty_sse_write(resp, gaty_make_chunk(cid, now, model, {}, "stop"))

    if stream_include_usage:
        await gaty_sse_write(resp, {"id": cid, "object": "chat.completion.chunk", "created": now, "model": model, "choices": [], "usage": usage})

    try:
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
    except Exception:
        pass
    return resp


# ==============================================================================
#  Gaty: OpenAI /v1/chat/completions handler
# ==============================================================================
async def gaty_handle_openai(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=GATY_CORS)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return gaty_error("Request body must be valid JSON.", 400)

    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return gaty_error("'messages' must be a non-empty array.", 400)

    return await gaty_core_complete(
        model              = body.get("model") or "deepseek-chat",
        messages           = messages,
        tools              = body.get("tools"),
        tool_choice        = body.get("tool_choice"),
        stream             = bool(body.get("stream", False)),
        stream_include_usage = bool((body.get("stream_options") or {}).get("include_usage")),
        request            = request,
    )


# ==============================================================================
#  Gaty: Anthropic /v1/messages handler (used by Claude CLI)
# ==============================================================================
def gaty_anthropic_to_openai_messages(anthropic_messages: list[dict], system: str) -> list[dict]:
    # Convert Anthropic message format to OpenAI message format
    result = []
    if system:
        result.append({"role": "system", "content": system})
    for msg in anthropic_messages:
        role    = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        parts.append(str(block.get("content", "")))
                else:
                    parts.append(str(block))
            content = "\n".join(parts)
        result.append({"role": role, "content": content})
    return result


def gaty_anthropic_to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    # Convert Anthropic tool definitions to OpenAI function format
    return [{
        "type": "function",
        "function": {
            "name":        tool.get("name", ""),
            "description": tool.get("description", ""),
            "parameters":  tool.get("input_schema") or tool.get("parameters") or {},
        }
    } for tool in anthropic_tools]


async def gaty_handle_anthropic(request: web.Request) -> web.Response:
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=GATY_CORS)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return gaty_error("Request body must be valid JSON.", 400)

    # Extract Anthropic-specific fields
    system = body.get("system") or ""
    if isinstance(system, list):
        system = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)

    openai_messages = gaty_anthropic_to_openai_messages(body.get("messages") or [], system)
    anthropic_tools = body.get("tools") or []
    openai_tools    = gaty_anthropic_to_openai_tools(anthropic_tools) if anthropic_tools else None
    model           = body.get("model") or "deepseek-chat"
    stream          = bool(body.get("stream", False))

    # Use the shared core to generate the response
    openai_resp = await gaty_core_complete(
        model              = model,
        messages           = openai_messages,
        tools              = openai_tools,
        tool_choice        = None,
        stream             = stream,
        stream_include_usage = False,
        request            = request,
    )

    # If streaming, the core already returns Anthropic-compatible SSE (good enough for Claude CLI)
    # If not streaming, translate OpenAI JSON back to Anthropic JSON
    if stream:
        # For streaming, we need to emit Anthropic SSE events
        # Re-run the core logic in Anthropic SSE mode
        is_reasoner = "reasoner" in model.lower() or "r1" in model.lower()
        tool_names:  list[str]       = []
        tool_schemas: dict[str, dict] = {}
        if openai_tools:
            for tool in openai_tools:
                fn   = tool.get("function", {})
                name = fn.get("name")
                if name:
                    tool_names.append(name)
                    tool_schemas[name] = fn.get("parameters") or {}

        prompt, mode = gaty_build_prompt(openai_messages, openai_tools, None)
        result = await gaty_run_job(prompt, is_reasoner)

        if "error" in result:
            return gaty_error(result["error"], 502, "server_error")

        raw_text  = (result.get("text")      or "").strip()
        reasoning = (result.get("reasoning") or "").strip()

        calls: list[dict] = []
        content_text: str = ""

        if mode == "native_tools" and tool_names:
            parsed = gaty_parse_tool_calls(raw_text, tool_names)
            if parsed:
                for call in parsed:
                    args = gaty_coerce_args(call["arguments"], tool_schemas.get(call["name"]))
                    calls.append({"name": call["name"], "arguments": args})
            else:
                content_text = gaty_ensure_fenced(raw_text)
        else:
            content_text = gaty_ensure_fenced(raw_text)

        accumulated = int(result.get("accumulatedTokens") or 0)
        initial     = int(result.get("initialTokens")     or 0)
        usage       = gaty_usage(prompt, raw_text, accumulated, initial)

        msg_id = f"msg_{uuid.uuid4().hex[:24]}"
        now    = int(time.time())

        resp = web.StreamResponse(status=200, reason="OK", headers={
            **GATY_CORS,
            "Content-Type":  "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "Connection":    "keep-alive",
        })
        await resp.prepare(request)

        async def anthropic_sse(event: str, data: dict) -> None:
            # Write Anthropic SSE event — silently drop if client disconnected
            try:
                payload = json.dumps(data, ensure_ascii=False)
                await resp.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
            except (ClientConnectionResetError, ConnectionResetError):
                pass
            except Exception as exc:
                if "closing transport" in str(exc):
                    pass
                else:
                    raise

        await anthropic_sse("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": 0},
        }})

        if calls:
            for idx, call in enumerate(calls):
                tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
                await anthropic_sse("content_block_start", {"type": "content_block_start", "index": idx,
                    "content_block": {"type": "tool_use", "id": tool_id, "name": call["name"], "input": {}}})
                for frag in gaty_iter_chunks(json.dumps(call["arguments"], ensure_ascii=False)):
                    await anthropic_sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                        "delta": {"type": "input_json_delta", "partial_json": frag}})
                await anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": idx})
            stop_reason = "tool_use"
        else:
            await anthropic_sse("content_block_start", {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}})
            for frag in gaty_iter_chunks(content_text or "", size=64):
                await anthropic_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": frag}})
            await anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
            stop_reason = "end_turn"

        await anthropic_sse("message_delta", {"type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage["completion_tokens"]}})
        await anthropic_sse("message_stop", {"type": "message_stop"})
        try:
            await resp.write_eof()
        except Exception:
            pass
        return resp

    # Non-streaming: translate OpenAI response to Anthropic format
    if openai_resp.status != 200:
        return openai_resp

    try:
        oai = json.loads(openai_resp.body)
    except Exception:
        return openai_resp

    choice  = (oai.get("choices") or [{}])[0]
    msg     = choice.get("message") or {}
    oai_use = oai.get("usage") or {}

    content_blocks = []
    stop_reason    = "end_turn"

    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            fn   = tc.get("function") or {}
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                try:    args = json.loads(args)
                except: args = {}
            content_blocks.append({"type": "tool_use", "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                "name": fn.get("name", ""), "input": args})
        stop_reason = "tool_use"
    else:
        content_blocks.append({"type": "text", "text": msg.get("content") or ""})

    return web.json_response({
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant", "model": model,
        "content":       content_blocks,
        "stop_reason":   stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": oai_use.get("prompt_tokens", 0), "output_tokens": oai_use.get("completion_tokens", 0)},
    }, headers=GATY_CORS)


# ==============================================================================
#  Gaty: aiohttp app wiring
# ==============================================================================
def gaty_make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)

    # OpenAI-compatible endpoints (Cline, Continue, Aider, etc.)
    app.router.add_route("POST",    "/v1/chat/completions", gaty_handle_openai)
    app.router.add_route("OPTIONS", "/v1/chat/completions", gaty_handle_openai)
    app.router.add_route("GET",     "/v1/models",           gaty_handle_models)
    app.router.add_route("OPTIONS", "/v1/models",           gaty_handle_models)

    # Anthropic-compatible endpoints (Claude CLI)
    app.router.add_route("POST",    "/v1/messages",         gaty_handle_anthropic)
    app.router.add_route("OPTIONS", "/v1/messages",         gaty_handle_anthropic)

    # Utility
    app.router.add_get("/health", gaty_handle_health)
    app.router.add_get("/ws",     gaty_websocket_handler)

    return app


# ==============================================================================
#  Gaty: entrypoint
# ==============================================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gaty — DeepSeek Web Bridge Proxy 💜")
    parser.add_argument("--host",            default=GATY_HOST,          help=f"Bind host (default: {GATY_HOST})")
    parser.add_argument("--port",            type=int, default=GATY_PORT, help=f"Bind port (default: {GATY_PORT})")
    parser.add_argument("--reset-threshold", type=int, default=GATY_AUTO_RESET_TOKENS,
                        help=f"Auto-reset token threshold (default: {GATY_AUTO_RESET_TOKENS}, 0=off)")
    args = parser.parse_args()
    GATY_AUTO_RESET_TOKENS = args.reset_threshold

    print("\033[95m")
    print("  ██████╗  █████╗ ████████╗██╗   ██╗")
    print(" ██╔════╝ ██╔══██╗╚══██╔══╝╚██╗ ██╔╝")
    print(" ██║  ███╗███████║   ██║    ╚████╔╝ ")
    print(" ██║   ██║██╔══██║   ██║     ╚██╔╝  ")
    print(" ╚██████╔╝██║  ██║   ██║      ██║   ")
    print("  ╚═════╝ ╚═╝  ╚═╝   ╚═╝      ╚═╝   ")
    print("\033[0m")
    print(f"\033[95m[Gaty]\033[0m OpenAI + Anthropic bridge on \033[96mhttp://{args.host}:{args.port}\033[0m")
    print(f"\033[95m[Gaty]\033[0m WebSocket endpoint: \033[96mws://{args.host}:{args.port}/ws\033[0m")
    print(f"\033[95m[Gaty]\033[0m Health check:       \033[96mhttp://{args.host}:{args.port}/health\033[0m")
    print()

    web.run_app(gaty_make_app(), host=args.host, port=args.port, print=None)
