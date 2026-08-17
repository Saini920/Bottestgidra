// Session blob decryption (blueprint Section 9.3).
//
// The browser encrypts the mtcute string session as:
//   blob = base64( iv(16) || authTag(16) || AES-256-GCM-ciphertext )
// with a key derived from the user's passphrase.
// The worker decrypts with SESSION_KEY (a GitHub Actions secret) — same
// derivation: SHA-256(passphrase). The plaintext session NEVER touches
// disk, env, subprocess args or logs — it lives only in process memory.

import { createDecipheriv, createHash } from "node:crypto";

/**
 * Decrypt an AES-256-GCM session blob.
 * @param {string} blobB64 base64 blob from the dispatch payload
 * @param {string} key raw passphrase (SESSION_KEY secret)
 * @returns {string} plaintext mtcute string session
 */
export function decryptSessionBlob(blobB64, key) {
  if (!blobB64) throw new Error("session blob is empty");
  if (!key) throw new Error("SESSION_KEY secret is missing");

  const buf = Buffer.from(blobB64, "base64");
  if (buf.length < 32) throw new Error("session blob is too short (corrupt?)");

  const iv = buf.subarray(0, 16);
  const authTag = buf.subarray(16, 32);
  const data = buf.subarray(32);

  const keyBuf = createHash("sha256").update(key, "utf8").digest();
  const decipher = createDecipheriv("aes-256-gcm", keyBuf, iv);
  decipher.setAuthTag(authTag);

  try {
    return Buffer.concat([decipher.update(data), decipher.final()]).toString("utf8");
  } catch {
    throw new Error("session decrypt failed — SESSION_KEY mismatch or tampered blob");
  }
}
