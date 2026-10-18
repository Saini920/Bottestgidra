#!/usr/bin/env node
/**
 * MCP SSE Bridge
 * Exposes a standard stdio-based MCP server over HTTP Server-Sent Events (SSE).
 * Compatible with Claude Desktop, Cursor, Antigravity, and all standard MCP clients.
 * Uses only Node.js built-in modules - zero external dependencies required.
 */

import http from "node:http";
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import { URL } from "node:url";

const PORT = parseInt(process.env.PORT || "8080", 10);
const HOST = process.env.HOST || "0.0.0.0";
const MCP_COMMAND = process.env.MCP_COMMAND || "npx";
const MCP_ARGS = process.env.MCP_ARGS ? process.env.MCP_ARGS.split(" ") : ["-y", "rea-agents", "mcp"];

// Map to track active sessions: sessionId -> { child, res, createdAt }
const sessions = new Map();

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

    // Send 1KB padding comment to force Cloudflare/Nginx proxies to flush stream immediately
    res.write(":" + " ".repeat(1024) + "\r\n\r\n");

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
        if (session.child && session.child.stdin.writable) {
          session.child.stdin.write(body.trim() + "\n");
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
}, 25000);

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
