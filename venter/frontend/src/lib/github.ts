// GitHub REST API helpers — used directly from the browser (CORS-enabled).

export interface TestResult {
  ok: boolean;
  message: string;
}

/** Validate token + repo + dispatch permission (Settings → Test Connection). */
export async function testConnection(token: string, repo: string): Promise<TestResult> {
  if (!token) return { ok: false, message: "GITHUB_TOKEN is empty" };
  if (!/^[^/]+\/[^/]+$/.test(repo)) return { ok: false, message: "GITHUB_REPO must be owner/name" };

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "venter",
  };

  try {
    const rate = await fetch("https://api.github.com/rate_limit", { headers });
    if (rate.status === 401) return { ok: false, message: "Token invalid (401) — check the token" };
    if (rate.status === 403) return { ok: false, message: "Token rate-limited or no permission (403)" };

    const repoRes = await fetch(`https://api.github.com/repos/${repo}`, { headers });
    if (repoRes.status === 404) return { ok: false, message: `Repo ${repo} not found or token has no access` };

    const data = await repoRes.json();
    return { ok: true, message: `Connected ✅ — ${data.full_name} (${data.visibility})` };
  } catch (e: any) {
    return { ok: false, message: `Network error: ${e?.message || e}` };
  }
}

export interface DispatchPayload {
  file_message_id: number;
  filename: string;
  session: string; // encrypted blob
  job_id: string;
  is_admin?: boolean;
  is_premium?: boolean;
  user_id?: string;
}

/** Trigger a repository_dispatch for one engine. */
export async function dispatchJob(
  token: string,
  repo: string,
  eventType: string,
  payload: DispatchPayload
): Promise<{ ok: boolean; message: string }> {
  try {
    const resp = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "venter",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ event_type: eventType, client_payload: payload }),
    });
    if (resp.status === 204 || resp.status === 200) return { ok: true, message: "Job dispatched" };
    const text = await resp.text();
    return { ok: false, message: `Dispatch failed (HTTP ${resp.status}): ${text.slice(0, 200)}` };
  } catch (e: any) {
    return { ok: false, message: `Network error: ${e?.message || e}` };
  }
}
