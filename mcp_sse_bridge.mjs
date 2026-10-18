#!/usr/bin/env node
/**
 * MCP SSE Bridge
 * Exposes a standard stdio-based MCP server over HTTP Server-Sent Events (SSE).
 * Compatible with Claude Desktop, Cursor, Antigravity, and all standard MCP clients.
 * Uses only Node.js built-in modules - zero external dependencies required.
 *
 * Live Telegram Stream:
 * Intercepts MCP JSON-RPC messages and sends sequential real-time updates to Telegram:
 * - Client connection & handshake
 * - Tool execution requests with parameters
 * - Tool completion & execution duration
 * - Tool errors & warnings
 */

import http from "node:http";
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import { URL } from "node:url";

const PORT = parseInt(process.env.PORT || "8080", 10);
const HOST = process.env.HOST || "0.0.0.0";
const MCP_COMMAND = process.env.MCP_COMMAND || "npx";
const MCP_ARGS = process.env.MCP_ARGS ? process.env.MCP_ARGS.split(" ") : ["-y", "rea-agents", "mcp"];

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || "";
const CHAT_ID = process.env.PAYLOAD_CHAT_ID || "";

// Map to track active sessions: sessionId -> { child, res, createdAt }
const sessions = new Map();

// Map to track pending tool calls: id -> { name, startTime }
const pendingToolCalls = new Map();

function escapeHtml(text) {
  if (!text) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function tgNotify(text) {
  if (!BOT_TOKEN || !CHAT_ID) return;
  try {
    const res = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: CHAT_ID,
        text: text,
        parse_mode: "HTML",
        disable_web_page_preview: true,
      }),
    });
    if (!res.ok) {
      console.warn(`[TG Notify] Response not ok: ${res.status}`);
    }
  } catch (err) {
    console.warn(`[TG Notify] Error: ${err.message}`);
  }
}

function handleJsonRpcRequest(rawBody) {
  try {
    const msg = JSON.parse(rawBody);
    if (!msg || typeof msg !== "object") return;

    if (msg.method === "initialize") {
      const client = msg.params?.clientInfo?.name || "AI Agent";
      const ver = msg.params?.clientInfo?.version || "";
      tgNotify(
        `🟢 <b>AI Agent Connected!</b>\n` +
        `👤 Client: <code>${escapeHtml(client)} ${escapeHtml(ver)}</code>\n` +
        `⚡ REA MCP tools are now active and ready.`
      );
    } else if (msg.method === "tools/call") {
      const toolName = msg.params?.name || "unknown_tool";
      const args = msg.params?.arguments || {};
      let argsPreview = JSON.stringify(args);
      if (argsPreview.length > 250) {
        argsPreview = argsPreview.slice(0, 247) + "...";
      }

      if (msg.id !== undefined) {
        pendingToolCalls.set(String(msg.id), {
          name: toolName,
          startTime: Date.now(),
        });
      }

      tgNotify(
        `🛠️ <b>[MCP Tool Executing]</b>\n` +
        `⚙️ <b>Tool:</b> <code>${escapeHtml(toolName)}</code>\n` +
        `📝 <i>Args:</i> <code>${escapeHtml(argsPreview)}</code>`
      );
    } else if (msg.method === "resources/read") {
      const uri = msg.params?.uri || "";
      tgNotify(`📖 <b>[MCP Resource Read]</b> <code>${escapeHtml(uri)}</code>`);
    }
  } catch (_) {
    // Non-JSON or partial chunk, ignore
  }
}

function handleJsonRpcResponse(rawLine) {
  try {
    const resp = JSON.parse(rawLine);
    if (!resp || typeof resp !== "object" || resp.id === undefined) return;

    const idStr = String(resp.id);
    if (pendingToolCalls.has(idStr)) {
      const pending = pendingToolCalls.get(idStr);
      pendingToolCalls.delete(idStr);

      const elapsed = ((Date.now() - pending.startTime) / 1000).toFixed(1);

      if (resp.error) {
        const errMsg = resp.error.message || JSON.stringify(resp.error);
        tgNotify(
          `❌ <b>[MCP Tool Error]</b> <code>${escapeHtml(pending.name)}</code> (${elapsed}s)\n` +
          `⚠️ <i>Details:</i> <code>${escapeHtml(String(errMsg).slice(0, 300))}</code>`
        );
      } else {
        // Result summary
        let resultSummary = "";
        if (resp.result?.content && Array.isArray(resp.result.content)) {
          const first = resp.result.content[0];
          if (first && first.text) {
            resultSummary = first.text.trim().slice(0, 150).replace(/\n/g, " ");
            if (first.text.length > 150) resultSummary += "...";
          }
        }

        let msg = `✅ <b>[MCP Tool Completed]</b> <code>${escapeHtml(pending.name)}</code> (${elapsed}s)`;
        if (resultSummary) {
          msg += `\n📄 <i>Output preview:</i> <code>${escapeHtml(resultSummary)}</code>`;
        }
        tgNotify(msg);
      }
    }
  } catch (_) {
    // Non-JSON response, ignore
  }
}

function sendJson(res, statusCode, data) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
  });
  res.end(JSON.stringify(data));
}

const server = http.createServer((req, res) => {
  const reqUrl = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const pathname = reqUrl.pathname;

  // Handle CORS Preflight
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    });
    res.end();
    return;
  }

  // Health / Info endpoint
  if (req.method === "GET" && (pathname === "/" || pathname === "/health")) {
    sendJson(res, 200, {
      status: "online",
      server: "REA-MCP-SSE-Bridge",
      activeSessions: sessions.size,
      command: `${MCP_COMMAND} ${MCP_ARGS.join(" ")}`,
      uptime: process.uptime(),
    });
    return;
  }

  // SSE Transport connection: GET /sse
  if (req.method === "GET" && pathname === "/sse") {
    const sessionId = crypto.randomUUID();

    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "Access-Control-Allow-Origin": "*",
      "X-Accel-Buffering": "no",
    });
    if (typeof res.flushHeaders === "function") res.flushHeaders();

    // Send 8KB padding comment to force Cloudflare/Nginx proxies to flush stream immediately
    res.write(":" + " ".repeat(4096) + "\r\n\r\n");
    res.write(":" + " ".repeat(4096) + "\r\n\r\n");

    // Send endpoint event with session message URL
    const messageEndpoint = `/message?sessionId=${sessionId}`;
    res.write(`event: endpoint\r\ndata: ${messageEndpoint}\r\n\r\n`);
    console.log(`[SSE] Session initialized: ${sessionId}`);

    // Spawn the stdio MCP server process
    const child = spawn(MCP_COMMAND, MCP_ARGS, {
      stdio: ["pipe", "pipe", "inherit"],
      env: {
        ...process.env,
        FORCE_COLOR: "0",
      },
    });

    sessions.set(sessionId, { child, res, createdAt: Date.now() });

    let stdoutBuffer = "";
    child.stdout.on("data", (chunk) => {
      stdoutBuffer += chunk.toString("utf8");
      const lines = stdoutBuffer.split(/\r?\n/);
      // Keep unfinished partial line in buffer
      stdoutBuffer = lines.pop();

      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed) {
          // Check for JSON-RPC tool response
          handleJsonRpcResponse(trimmed);
          // SSE data frame
          res.write(`event: message\r\ndata: ${trimmed}\r\n\r\n`);
        }
      }
    });

    child.on("error", (err) => {
      console.error(`[MCP] Process error for session ${sessionId}:`, err);
      res.write(`event: error\r\ndata: ${JSON.stringify({ error: err.message })}\r\n\r\n`);
      res.end();
    });

    child.on("exit", (code, signal) => {
      console.log(`[MCP] Process exited for session ${sessionId} (code=${code}, signal=${signal})`);
      sessions.delete(sessionId);
      res.end();
    });

    // Client disconnection handler
    req.on("close", () => {
      console.log(`[SSE] Client disconnected for session: ${sessionId}`);
      sessions.delete(sessionId);
      tgNotify(`ℹ️ <b>AI Agent Disconnected</b> from REA MCP Session.`);
      if (child && !child.killed) {
        child.kill("SIGTERM");
        setTimeout(() => {
          if (!child.killed) child.kill("SIGKILL");
        }, 3000);
      }
    });

    return;
  }

  // Message Receiver endpoint: POST /message
  if (req.method === "POST" && pathname === "/message") {
    const sessionId = reqUrl.searchParams.get("sessionId");
    if (!sessionId || !sessions.has(sessionId)) {
      sendJson(res, 404, { error: "Session not found or expired" });
      return;
    }

    const session = sessions.get(sessionId);
    let body = "";

    req.on("data", (chunk) => {
      body += chunk.toString("utf8");
    });

    req.on("end", () => {
      try {
        const trimmed = body.trim();
        // Parse and notify tool activity
        handleJsonRpcRequest(trimmed);

        if (session.child && session.child.stdin.writable) {
          session.child.stdin.write(trimmed + "\n");
          res.writeHead(202, {
            "Content-Type": "text/plain",
            "Access-Control-Allow-Origin": "*",
          });
          res.end("Accepted");
        } else {
          sendJson(res, 500, { error: "Session child process stdin is not writable" });
        }
      } catch (err) {
        console.error(`[MCP] Write error for session ${sessionId}:`, err);
        sendJson(res, 500, { error: err.message });
      }
    });

    return;
  }

  sendJson(res, 404, { error: "Not Found" });
});

// Periodic keepalive / heartbeat comment to prevent idle disconnects
setInterval(() => {
  for (const [sessionId, session] of sessions.entries()) {
    try {
      session.res.write(": keepalive\r\n\r\n");
    } catch (e) {
      sessions.delete(sessionId);
    }
  }
}, 15000);

server.listen(PORT, HOST, () => {
  console.log(`===============================================`);
  console.log(`  REA MCP SSE Bridge listening on ${HOST}:${PORT}`);
  console.log(`  SSE Endpoint: http://${HOST}:${PORT}/sse`);
  console.log(`  Target Command: ${MCP_COMMAND} ${MCP_ARGS.join(" ")}`);
  console.log(`===============================================`);
});

// Handle graceful shutdown
function handleShutdown() {
  console.log("\nShutting down REA MCP SSE Bridge...");
  for (const [sessionId, session] of sessions.entries()) {
    try {
      if (session.child && !session.child.killed) {
        session.child.kill("SIGTERM");
      }
      session.res.end();
    } catch (_) {}
  }
  server.close(() => {
    process.exit(0);
  });
}

process.on("SIGTERM", handleShutdown);
process.on("SIGINT", handleShutdown);
