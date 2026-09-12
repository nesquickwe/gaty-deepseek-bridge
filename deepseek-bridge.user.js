// ==UserScript==
// @name         Gaty — DeepSeek Bridge
// @namespace    https://github.com/nesquickwe/gaty-deepseek-bridge
// @version      3.0.0
// @description  Bridges chat.deepseek.com to the local Gaty proxy (ws://127.0.0.1:1337/ws)
// @match        https://chat.deepseek.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  // Guard against double-injection
  if (window.__GATY_BRIDGE_INITIALIZED__) {
    console.log("[Gaty] Already running, skipping re-init.");
    return;
  }
  window.__GATY_BRIDGE_INITIALIZED__ = true;

  // ── Gaty config ─────────────────────────────────────────────────────────────
  const GATY_WS_URL        = "ws://127.0.0.1:1337/ws";  // Gaty proxy WebSocket URL
  const GATY_TIMEOUT_MS    = 360_000;                    // 6 min job timeout
  const GATY_POLL_MS       = 300;                        // DOM poll interval
  const GATY_STABLE_MS     = 800;                        // ms of stable text = done
  const GATY_PURPLE        = "#a855f7";                  // Gaty brand purple
  const GATY_GREEN         = "#10b981";
  const GATY_YELLOW        = "#f59e0b";
  const GATY_RED           = "#ef4444";

  // ── Gaty logger ─────────────────────────────────────────────────────────────
  const gatyLog = (msg, color = GATY_PURPLE) =>
    console.log(`%c[Gaty] ${msg}`, `color:${color};font-weight:bold;`);

  // ── Gaty status badge ───────────────────────────────────────────────────────
  const gatyBadge = document.createElement("div");
  gatyBadge.id    = "gaty-bridge-badge";
  gatyBadge.title = "Gaty Bridge — click to reconnect";
  Object.assign(gatyBadge.style, {
    position:        "fixed",
    bottom:          "16px",
    right:           "16px",
    zIndex:          "999999",
    padding:         "6px 14px",
    borderRadius:    "20px",
    fontFamily:      "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    fontSize:        "12px",
    fontWeight:      "700",
    color:           "#fff",
    backgroundColor: "rgba(88, 28, 135, 0.92)",   // deep purple background
    backdropFilter:  "blur(8px)",
    border:          "1px solid rgba(168, 85, 247, 0.5)",
    boxShadow:       "0 0 16px rgba(168, 85, 247, 0.4)",
    display:         "flex",
    alignItems:      "center",
    gap:             "8px",
    cursor:          "pointer",
    userSelect:      "none",
    transition:      "all 0.2s ease",
  });

  const gatyDot = document.createElement("span");
  Object.assign(gatyDot.style, {
    width:           "8px",
    height:          "8px",
    borderRadius:    "50%",
    backgroundColor: GATY_RED,
    transition:      "background-color 0.2s ease",
    boxShadow:       "0 0 6px currentColor",
  });

  const gatyBadgeLabel = document.createElement("span");
  gatyBadgeLabel.textContent = "Gaty: Disconnected";

  gatyBadge.appendChild(gatyDot);
  gatyBadge.appendChild(gatyBadgeLabel);

  // Append badge once DOM is ready
  const gatyAttachBadge = () => {
    if (!gatyBadge.parentElement && document.body) document.body.appendChild(gatyBadge);
  };
  gatyAttachBadge();
  window.addEventListener("DOMContentLoaded", gatyAttachBadge);

  // Update badge color and label based on state
  function gatySetBadge(state, label) {
    gatyAttachBadge();
    const colorMap = { connected: GATY_GREEN, busy: GATY_YELLOW, disconnected: GATY_RED };
    gatyDot.style.backgroundColor  = colorMap[state] || GATY_RED;
    gatyBadgeLabel.textContent = label || `Gaty: ${state}`;
  }

  // ── Gaty: find DeepSeek's chat textarea ─────────────────────────────────────
  function gatyGetInput() {
    return (
      document.querySelector("textarea#chat-input") ||
      document.querySelector("textarea") ||
      document.querySelector('div[contenteditable="true"]')
    );
  }

  // ── Gaty: find DeepSeek's send button ───────────────────────────────────────
  function gatyGetSendBtn() {
    return (
      document.querySelector('button[aria-label="Send message"]') ||
      document.querySelector('div[role="button"]._52c986b') ||
      document.querySelector("div.ds-button--primary") ||
      (() => {
        const area = gatyGetInput();
        if (!area) return null;
        const form = area.closest("form");
        return form
          ? form.querySelector('button[type="submit"]') || form.querySelector("button:not([disabled])")
          : null;
      })()
    );
  }

  // ── Gaty: set textarea value via React's synthetic event system ─────────────
  function gatySetNativeValue(el, value) {
    const descriptor = Object.getOwnPropertyDescriptor(
      el.tagName === "TEXTAREA"
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype,
      "value"
    );
    if (descriptor) {
      descriptor.set.call(el, value);
    } else {
      el.value = value;
    }
    el.dispatchEvent(new Event("input",  { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ── Gaty: set contenteditable value ─────────────────────────────────────────
  function gatySetEditable(el, value) {
    el.focus();
    el.textContent = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
  }

  // ── Gaty: walk React fiber tree to find the onSubmit handler ────────────────
  function gatyFindSubmitFn() {
    const roots = [
      document.querySelector("textarea"),
      document.querySelector('div[contenteditable="true"]'),
      document.querySelector("#root"),
    ].filter(Boolean);

    for (const el of roots) {
      let node = el;
      while (node) {
        const fiberKey = Object.keys(node).find(
          k => k.startsWith("__reactFiber$") || k.startsWith("__reactInternalInstance$")
        );
        if (fiberKey && node[fiberKey]) {
          let fiber = node[fiberKey];
          while (fiber) {
            const props = fiber.memoizedProps || fiber.pendingProps;
            if (props && typeof props.onSubmit === "function") return props.onSubmit;
            fiber = fiber.return;
          }
        }
        node = node.parentElement;
      }
    }
    return null;
  }

  // ── Gaty: submit a prompt to DeepSeek ───────────────────────────────────────
  async function gatySubmit(prompt, thinkingEnabled) {
    // Primary: React fiber onSubmit (fastest, most reliable)
    const submitFn = gatyFindSubmitFn();
    if (submitFn) {
      gatyLog("Submitting via React fiber onSubmit", GATY_PURPLE);
      await submitFn(prompt, {
        thinkingEnabled:        Boolean(thinkingEnabled),
        searchEnabled:          false,
        uploadFileSupported:    true,
        interruptAndSendEnabled: true,
      });
      return;
    }

    // Fallback: type into input + click send
    const input = gatyGetInput();
    if (!input) throw new Error("Gaty: chat input not found — is DeepSeek loaded?");

    input.focus();
    await gatySleep(100);

    if (input.tagName === "TEXTAREA" || input.tagName === "INPUT") {
      gatySetNativeValue(input, prompt);
    } else {
      gatySetEditable(input, prompt);
    }

    await gatySleep(200);

    const btn = gatyGetSendBtn();
    if (btn && !btn.disabled) {
      gatyLog("Submitting via send button click", GATY_PURPLE);
      btn.click();
      return;
    }

    // Last resort: synthesized Enter keypress
    gatyLog("Submitting via Enter keypress", GATY_YELLOW);
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
    input.dispatchEvent(new KeyboardEvent("keyup",   { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
  }

  // ── Gaty: read last assistant response from DOM ──────────────────────────────
  function gatyGetLastResponse() {
    // Primary selector — confirmed working as of current DeepSeek UI
    const primary = document.querySelectorAll(".ds-markdown.ds-assistant-message-main-content");
    if (primary.length > 0) return (primary[primary.length - 1].innerText || "").trim();

    // Fallback chain
    for (const sel of [".ds-markdown", ".ds-message", ".ds-markdown-paragraph"]) {
      const els = document.querySelectorAll(sel);
      if (els.length > 0) return (els[els.length - 1].innerText || "").trim();
    }
    return "";
  }

  // ── Gaty: detect whether DeepSeek is still generating ───────────────────────
  function gatyIsGenerating() {
    // Loading/thinking indicator visible during generation
    if (
      document.querySelector('[class*="thinking"][class*="loading"]') ||
      document.querySelector('[class*="generating"]')
    ) return true;

    // The circle button is non-disabled only while generating (it becomes the stop button)
    if (document.querySelector(".ds-button--primary.ds-button--filled.ds-button--circle:not(.ds-button--disabled)"))
      return true;

    return false;
  }

  // ── Gaty: poll DOM until response is stable ──────────────────────────────────
  async function gatyWaitForResponse(beforeText, timeoutMs) {
    const deadline  = Date.now() + timeoutMs;
    let lastText    = beforeText;
    let stableAt    = null;
    let hasStarted  = false;

    // Give DeepSeek a moment to start before polling
    await gatySleep(800);

    while (Date.now() < deadline) {
      const current    = gatyGetLastResponse();
      const generating = gatyIsGenerating();

      if (!hasStarted) {
        // Wait for the response to actually begin appearing in DOM
        if (current !== beforeText && current.length > beforeText.length) {
          hasStarted = true;
          gatyLog("Response started streaming...", GATY_GREEN);
        } else {
          await gatySleep(GATY_POLL_MS);
          continue;
        }
      }

      if (current !== lastText) {
        // Text changed — reset stability timer
        lastText  = current;
        stableAt  = null;
      } else if (!generating) {
        // Text stable and not generating — start stability timer
        if (stableAt === null) {
          stableAt = Date.now();
        } else if (Date.now() - stableAt >= GATY_STABLE_MS) {
          gatyLog(`Response stable for ${GATY_STABLE_MS}ms — done! 💜`, GATY_GREEN);
          return current;
        }
      } else {
        // Still generating — reset timer
        stableAt = null;
      }

      await gatySleep(GATY_POLL_MS);
    }

    gatyLog("Timeout waiting for response — returning partial", GATY_YELLOW);
    return lastText || gatyGetLastResponse();
  }

  function gatySleep(ms) {
    return new Promise(r => setTimeout(r, ms));
  }

  // ── Gaty: delete current DeepSeek chat session ──────────────────────────────
  function gatyGetSessionId() {
    const m = window.location.pathname.match(/\/a\/chat\/s\/([a-zA-Z0-9_-]+)/);
    return m ? m[1] : null;
  }

  window.gatyDeleteChat = async () => {
    const sessionId = gatyGetSessionId();
    if (!sessionId) { gatyLog("Not in a chat session", GATY_YELLOW); return; }
    gatyLog(`Deleting session ${sessionId}...`, GATY_YELLOW);
    try {
      await fetch("/api/v0/chat_session/delete", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ chat_session_ids: [sessionId] }),
      });
      gatyLog("Chat deleted! Redirecting...", GATY_GREEN);
      setTimeout(() => { window.location.href = "/"; }, 150);
    } catch (e) {
      console.error("[Gaty] Failed to delete chat:", e);
    }
  };

  // Keep the old name working too
  window.deleteCurrentChat = window.gatyDeleteChat;

  // ── Gaty: WebSocket connection to proxy ──────────────────────────────────────
  let gatyWs            = null;
  let gatyReconnectTimer = null;
  let gatyJobRunning    = false;

  function gatyConnect() {
    if (gatyWs && (gatyWs.readyState === WebSocket.OPEN || gatyWs.readyState === WebSocket.CONNECTING)) return;
    if (gatyReconnectTimer) { clearTimeout(gatyReconnectTimer); gatyReconnectTimer = null; }

    const ws = new WebSocket(GATY_WS_URL);
    gatyWs = ws;

    ws.onopen = () => {
      gatyLog("Connected to Gaty proxy! 💜", GATY_GREEN);
      gatySetBadge("connected", "Gaty: Connected 💜");
    };

    ws.onclose = () => {
      if (gatyWs === ws) gatyWs = null;
      gatySetBadge("disconnected", "Gaty: Disconnected (click to retry)");
      gatyReconnectTimer = setTimeout(gatyConnect, 3000);
    };

    ws.onerror = () => gatySetBadge("disconnected", "Gaty: Connection Error");

    ws.onmessage = async (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch { return; }

      // Handle chat reset from proxy
      if (payload.action === "delete_chat") {
        gatyLog("Reset command from proxy", GATY_YELLOW);
        gatySetBadge("busy", "Gaty: Resetting...");
        try {
          await window.gatyDeleteChat();
          gatySetBadge("connected", "Gaty: Connected 💜");
          ws.send(JSON.stringify({ id: payload.id, success: true }));
        } catch (e) {
          gatySetBadge("connected", "Gaty: Connected 💜");
          ws.send(JSON.stringify({ id: payload.id, error: String(e) }));
        }
        return;
      }

      // Reject if a job is already in flight
      if (gatyJobRunning) {
        ws.send(JSON.stringify({ id: payload.id, error: "Gaty: bridge busy with another request." }));
        return;
      }

      gatyJobRunning = true;
      const { id, prompt, thinkingEnabled } = payload;
      gatyLog(`New job — ${prompt.length} chars`, GATY_YELLOW);
      gatySetBadge("busy", "Gaty: Generating... 💜");

      try {
        // Snapshot state before submitting so we can isolate the new reply
        const beforeCount = document.querySelectorAll(".ds-markdown.ds-assistant-message-main-content").length;
        const beforeText  = gatyGetLastResponse();

        // Fire the prompt at DeepSeek
        await gatySubmit(prompt, thinkingEnabled);
        gatyLog("Prompt submitted — waiting for response...", GATY_YELLOW);

        // Wait for completion
        const responseText = await gatyWaitForResponse(beforeText, GATY_TIMEOUT_MS);

        // Extract only the new reply (not prior messages)
        let newText    = responseText;
        const afterEls = document.querySelectorAll(".ds-markdown.ds-assistant-message-main-content");
        if (afterEls.length > beforeCount) {
          // A new message block appeared — grab just that one
          newText = (afterEls[afterEls.length - 1].innerText || "").trim();
        } else if (beforeText && responseText.startsWith(beforeText)) {
          newText = responseText.slice(beforeText.length).trim();
        }

        gatyLog(`Done! Got ${newText.length} chars 💜`, GATY_GREEN);
        gatySetBadge("connected", "Gaty: Connected 💜");

        ws.send(JSON.stringify({
          id,
          text:              newText,
          reasoning:         "",
          accumulatedTokens: 0,
          initialTokens:     0,
        }));
      } catch (err) {
        gatyLog(`Job error: ${err}`, GATY_RED);
        gatySetBadge("connected", "Gaty: Connected 💜");
        ws.send(JSON.stringify({ id, error: String(err) }));
      } finally {
        gatyJobRunning = false;
      }
    };
  }

  // Reconnect on badge click
  gatyBadge.addEventListener("click", () => {
    gatyLog("Manual reconnect...", GATY_YELLOW);
    gatyConnect();
  });

  // Boot!
  gatyConnect();
  gatyLog("Gaty bridge loaded 💜 Connecting to ws://127.0.0.1:1337/ws ...", GATY_PURPLE);
})();
