#!/usr/bin/env node
// Venter C/C++ compile worker — port of the old worker_cc_compile.py.
// Cross-compiles .c/.cpp → Android ARM64 .so with aarch64-linux-gnu-gcc.

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";

async function runCcCompile(inputPath, workDir, onProgress) {
  const stem = path.basename(inputPath, path.extname(inputPath));
  const outSo = path.join(workDir, `lib${stem}.so`);

  await onProgress(15, "⚙️ Compiling C/C++ → .so (ARM64)...");
  await exec("aarch64-linux-gnu-gcc", ["-shared", "-fPIC", "-O2", "-o", outSo, inputPath], (line) => {
    if (/error:/i.test(line)) return; // errors surface via exit code
  });
  if (!fs.existsSync(outSo)) throw new Error("gcc produced no .so");

  return [{ arcname: path.basename(outSo), path: outSo }];
}

runMain(() =>
  runWorker({
    engine: "CC-Compile",
    zipSuffix: "so",
    run: runCcCompile,
    batchExts: [".c", ".cpp", ".cc", ".cxx"],
  })
);
