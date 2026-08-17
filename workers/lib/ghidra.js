// Ghidra runner — port of worker.py run_ghidra + apply_memory_settings +
// proc_cpu_usage + extract_error_info. The actual heavy lifting is done by
// Ghidra's analyzeHeadless (Java); this module just orchestrates it.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const GHIDRA_HOME = process.env.GHIDRA_HOME || "/opt/ghidra";

/** Parse "4G" / "512M" → number in GB (port of parse_mem_gb). */
function parseMemGb(value) {
  const m = /^(\d+(?:\.\d+)?)/.exec(value || "");
  return m ? Number(m[0]) : 4;
}

/**
 * Cap JAVA_MAX_MEM to available RAM and write it into launch.properties
 * (port of apply_memory_settings). Ghidra ships JAVA_MAX_MEM commented out
 * and silently runs with a tiny heap otherwise.
 */
export function applyMemorySettings(ghidraHome = GHIDRA_HOME) {
  const requested = process.env.JAVA_MAX_MEM || "4G";
  let mem = requested;

  const avail = os.totalmem() / 1024 ** 3; // GB
  const cap = Math.floor(avail - 1); // reserve ~1GB for native decompiler
  const req = parseMemGb(requested);
  if (cap >= 1 && req > cap) {
    mem = `${cap}G`;
    console.warn(`Capped JAVA_MAX_MEM ${requested} -> ${mem} (available RAM ${avail.toFixed(1)} GB)`);
  }

  const props = path.join(ghidraHome, "support", "launch.properties");
  try {
    let text = fs.readFileSync(props, "utf8");
    const lineRe = /^[ \t]*[#!]?[ \t]*JAVA_MAX_MEM\s*=.*$/m;
    const has = lineRe.test(text);
    text = has
      ? text.replace(lineRe, `JAVA_MAX_MEM=${mem}`)
      : text.replace(/\s*$/, "\n") + `JAVA_MAX_MEM=${mem}\n`;
    fs.writeFileSync(props, text);
    console.log(`JAVA_MAX_MEM -> ${mem}`);
  } catch (e) {
    console.warn("Could not set JAVA_MAX_MEM:", e.message);
  }
}

/** CPU ticks from /proc/<pid>/stat (Linux — GitHub runners are Linux). */
function procCpuUsage(pid) {
  try {
    const parts = fs.readFileSync(`/proc/${pid}/stat`, "utf8").split(" ");
    return Number(parts[13]) + Number(parts[14]);
  } catch {
    return -1;
  }
}

const ERROR_KEYS = [
  "error", "exception", "failed", "unable", "cannot", "could not",
  "unsupported", "not recognized", "no language", "report", "caused by",
  "fatal", "import", "invalid", "unknown",
];

/** Extract the most relevant error lines from Ghidra output tail. */
export function extractErrorInfo(lines) {
  const list = lines || [];
  const hits = list.filter((ln) => ERROR_KEYS.some((k) => ln.toLowerCase().includes(k)));
  return hits.slice(-16).join("\n") || list.slice(-40).join("\n");
}

/**
 * Run analyzeHeadless on one file.
 * @param {{filePath: string, workDir: string, onProgress: (pct:number,label:string)=>Promise<void>|void, disableCallFixup?: boolean, scriptDir?: string, ghidraHome?: string}} opts
 * @returns {Promise<{outC: string, outMeta: string, tail: string, lines: string[], returncode: number}>}
 */
export async function runGhidra({ filePath, workDir, onProgress, disableCallFixup = false, scriptDir, ghidraHome = GHIDRA_HOME }) {
  const analyzeHeadless = path.join(ghidraHome, "support", "analyzeHeadless");
  const projectDir = path.join(workDir, "project");

  // Crash-retry reuses the same workDir; a stale project dir breaks mkdir.
  fs.rmSync(projectDir, { recursive: true, force: true });
  fs.mkdirSync(projectDir, { recursive: true });

  const outC = path.join(workDir, "decompiled.c");
  const outMeta = path.join(workDir, "info.txt");

  const cmd = [analyzeHeadless, projectDir, "Proj", "-overwrite"];
  if (disableCallFixup) cmd.push("-preScript", "DisableCallFixup");
  cmd.push(
    "-import", filePath,
    "-scriptPath", scriptDir,
    "-postScript", "DecompileAll.java",
    outC, outMeta,
    "-deleteProject"
  );
  console.log("Running:", cmd.join(" "));

  const proc = spawn(cmd[0], cmd.slice(1), { stdio: ["ignore", "pipe", "pipe"] });

  const lines = [];
  const tail = [];
  let lastActivity = Date.now();
  let lastCpu = procCpuUsage(proc.pid);

  await onProgress(5, "📥 Importing file into Ghidra...");

  const output = new Promise((resolve, reject) => {
    let buf = "";
    const handle = (chunk) => {
      lastActivity = Date.now(); // any output = activity (same as Python readline reset)
      buf += chunk.toString("utf8");
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        handleLine(line);
      }
    };
    const handleLine = (line) => {
      lines.push(line);
      tail.push(line);
      if (tail.length > 250) tail.shift();

      const low = line.toLowerCase();
      if (low.includes("analyzing") || low.includes("processing")) {
        onProgress(20, "🔧 Analyzing binary with Ghidra...");
      }
      const m = /DECOMP_PROGRESS\s+(\d+)\/(\d+)/.exec(line);
      if (m) {
        const done = Number(m[1]);
        const total = Number(m[2]);
        const pct = total ? Math.round(20 + 75 * (done / total)) : 20;
        onProgress(pct, `🧠 Decompiling functions ${done}/${total}...`);
      }
    };

    proc.stdout.on("data", handle);
    proc.stderr.on("data", handle);

    proc.on("error", reject);
    proc.on("close", (code) => resolve(code));
  });

  // Stall watchdog: if no output for 60s, check CPU activity; kill after 30 min idle.
  const stallTimer = setInterval(() => {
    const cpu = procCpuUsage(proc.pid);
    if (cpu > lastCpu) {
      lastCpu = cpu;
      lastActivity = Date.now();
    } else if (Date.now() - lastActivity >= 30 * 60 * 1000) {
      proc.kill("SIGKILL");
      clearInterval(stallTimer);
      rejectWatchdog(new Error("Ghidra stalled: no CPU activity for 30 minutes"));
    }
  }, 15_000);

  let watchdogError = null;
  function rejectWatchdog(err) {
    watchdogError = err;
  }

  const timeout = setTimeout(() => {
    proc.kill("SIGKILL");
  }, 24 * 60 * 60 * 1000); // 24h hard cap (same as old 86400s)

  const rc = await output;
  clearInterval(stallTimer);
  clearTimeout(timeout);
  if (watchdogError) throw watchdogError;

  console.log("analyzeHeadless exit =", rc);
  const tailText = tail.slice(-50).join("\n");
  if (rc !== 0) {
    throw new Error(`Ghidra exited with code ${rc}:\n${extractErrorInfo(lines).slice(0, 1200)}`);
  }
  return { outC, outMeta, tail: tailText, lines, returncode: rc };
}
