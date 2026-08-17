// IndexedDB wrapper (blueprint Section 9.5 — IndexedDB instead of localStorage).
// Stores two records: "settings" and "session".

const DB_NAME = "venter";
const STORE = "kv";

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains(STORE)) {
        req.result.createObjectStore(STORE);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function get<T>(key: string): Promise<T | null> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).get(key);
    req.onsuccess = () => resolve((req.result as T) ?? null);
    req.onerror = () => reject(req.error);
    tx.oncomplete = () => db.close();
  });
}

async function set(key: string, value: unknown): Promise<void> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(value, key);
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
    tx.onerror = () => reject(tx.error);
  });
}

async function del(key: string): Promise<void> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(key);
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
    tx.onerror = () => reject(tx.error);
  });
}

import type { Settings, StoredSession } from "../types";

export const storage = {
  async getSettings(): Promise<Settings | null> {
    return get<Settings>("settings");
  },
  async saveSettings(s: Settings): Promise<void> {
    return set("settings", s);
  },
  async getSession(): Promise<StoredSession | null> {
    return get<StoredSession>("session");
  },
  async saveSession(s: StoredSession): Promise<void> {
    return set("session", s);
  },
  async clearSession(): Promise<void> {
    return del("session");
  },
};
