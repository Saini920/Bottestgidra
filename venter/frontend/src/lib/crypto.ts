// Browser-side session encryption — MUST match the worker's lib/crypto.js:
//   blob = base64( iv(16) || authTag(16) || ciphertext )
//   key  = SHA-256(passphrase)
// The passphrase is the user's SESSION_KEY (also stored as the GitHub Actions
// secret of the same name). The key is derived on the fly and never stored.

function bytesToBase64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

function base64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function deriveKey(passphrase: string): Promise<CryptoKey> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(passphrase));
  return crypto.subtle.importKey("raw", digest, "AES-GCM", false, ["encrypt", "decrypt"]);
}

/** Encrypt a string into the worker-compatible blob format. */
export async function encryptSession(plaintext: string, passphrase: string): Promise<string> {
  const key = await deriveKey(passphrase);
  const iv = crypto.getRandomValues(new Uint8Array(16)); // 16 bytes — matches worker
  const ct = new Uint8Array(
    await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, new TextEncoder().encode(plaintext))
  );
  // WebCrypto appends the 16-byte auth tag at the END of the ciphertext.
  const ciphertext = ct.subarray(0, ct.length - 16);
  const tag = ct.subarray(ct.length - 16);

  const blob = new Uint8Array(16 + 16 + ciphertext.length);
  blob.set(iv, 0);
  blob.set(tag, 16);
  blob.set(ciphertext, 32);
  return bytesToBase64(blob);
}

/** Decrypt a blob produced by encryptSession (used to re-load the session). */
export async function decryptSession(blobB64: string, passphrase: string): Promise<string> {
  const buf = base64ToBytes(blobB64);
  if (buf.length < 32) throw new Error("Session blob is too short (corrupt?)");
  const iv = buf.subarray(0, 16);
  const tag = buf.subarray(16, 32);
  const data = buf.subarray(32);
  const key = await deriveKey(passphrase);
  const ct = new Uint8Array(data.length + 16);
  ct.set(data, 0);
  ct.set(tag, data.length);
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ct);
  return new TextDecoder().decode(pt);
}
