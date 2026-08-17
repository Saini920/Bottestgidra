#!/usr/bin/env node
// Venter PDF→TXT worker — port of the old worker_pdf_txt.py.
// Extracts text with pdftotext; if the PDF has no text layer (scanned),
// falls back to tesseract OCR (eng+hin+urd+ben+...).

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";
import { walkFiles } from "./lib/zip.js";

const OCR_LANGS = "eng+hin+urd+ben+mar+guj+tam+tel+kan+mal+pan";

async function runPdfTxt(inputPath, workDir, onProgress) {
  const outTxt = path.join(workDir, "output.txt");

  await onProgress(10, "📄 Extracting text (pdftotext)...");
  try {
    await exec("pdftotext", ["-layout", inputPath, outTxt]);
  } catch {
    /* no text layer — OCR fallback below */
  }

  let text = fs.existsSync(outTxt) ? fs.readFileSync(outTxt, "utf8") : "";
  if (!text.trim()) {
    await onProgress(40, "🔍 No text layer — running OCR (tesseract)...");
    const pages = path.join(workDir, "page");
    await exec("pdftoppm", ["-png", "-r", "200", inputPath, pages]);
    const pngs = walkFiles(workDir).filter((f) => f.endsWith(".png")).sort();
    if (pngs.length === 0) throw new Error("PDF se pages render nahi hue");

    let ocr = "";
    for (let i = 0; i < pngs.length; i++) {
      await onProgress(40 + Math.round((i / pngs.length) * 50), `🔍 OCR page ${i + 1}/${pngs.length}...`);
      const part = path.join(workDir, `ocr_${i}`);
      await exec("tesseract", [pngs[i], part, "-l", OCR_LANGS, "--psm", "3"]);
      if (fs.existsSync(part + ".txt")) {
        ocr += `\n\n--- Page ${i + 1} ---\n\n` + fs.readFileSync(part + ".txt", "utf8");
      }
    }
    fs.writeFileSync(outTxt, ocr);
    text = ocr;
  }

  if (!text.trim()) throw new Error("PDF me koi text nahi mila");
  return [{ arcname: path.basename(outTxt), path: outTxt }];
}

runMain(() =>
  runWorker({
    engine: "PDF-TXT",
    zipSuffix: "txt",
    run: runPdfTxt,
    batchExts: [".pdf"],
  })
);
