// ZIP helpers for workers — listing, extracting and creating archives.
// Uses adm-zip (whole-file central directory read, per-entry decompression).

import AdmZip from "adm-zip";
import fs from "node:fs";
import path from "node:path";

const ZIP_MAGIC = Buffer.from([0x50, 0x4b, 0x03, 0x04]); // PK\x03\x04

/** Cheap magic check — is this a zip archive? (port of zipfile.is_zipfile) */
export function isZip(filePath) {
  try {
    const fd = fs.openSync(filePath, "r");
    const buf = Buffer.alloc(4);
    fs.readSync(fd, buf, 0, 4, 0);
    fs.closeSync(fd);
    return buf.equals(ZIP_MAGIC);
  } catch {
    return false;
  }
}

/**
 * Zip-slip protection (blueprint Section 9.6).
 * Returns a safe relative posix name, or null if the entry is malicious.
 */
export function sanitizeEntryName(entryName) {
  if (!entryName) return null;
  let name = entryName.replace(/\\/g, "/"); // normalize Windows separators
  if (name.startsWith("/")) return null; // absolute path
  const parts = name.split("/");
  for (const part of parts) {
    if (part === "..") return null; // path traversal
  }
  return name;
}

/** List zip entries: [{ name, size }] — reads central directory only. */
export function listZipEntries(zipPath) {
  const zip = new AdmZip(zipPath);
  return zip.getEntries().map((e) => ({
    name: e.entryName,
    size: e.header.size ?? 0, // uncompressed size
  }));
}

/**
 * Extract a zip with zip-slip protection.
 * @returns [{ name, filePath }] extracted files (directories skipped)
 */
export function extractZip(zipPath, destDir) {
  const zip = new AdmZip(zipPath);
  const out = [];
  fs.mkdirSync(destDir, { recursive: true });
  for (const entry of zip.getEntries()) {
    if (entry.isDirectory) continue;
    const safe = sanitizeEntryName(entry.entryName);
    if (!safe) {
      console.warn("zip-slip blocked entry:", JSON.stringify(entry.entryName));
      continue;
    }
    const filePath = path.join(destDir, safe);
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(filePath, zip.readFile(entry));
    out.push({ name: entry.entryName, filePath });
  }
  return out;
}

/**
 * Create a zip from files already on disk.
 * @param {string} zipPath output path
 * @param {{path: string, arcname: string}[]} files
 */
export function createZipFromFiles(zipPath, files) {
  const zip = new AdmZip();
  for (const f of files) {
    zip.addLocalFile(f.path, "", f.arcname);
  }
  zip.writeZip(zipPath);
  return zipPath;
}
