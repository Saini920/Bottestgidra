"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import LoginWizard from "@/components/LoginWizard";
import { api } from "@/lib/api";

export default function Home() {
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    api("/api/setup/status")
      .then((s) => {
        if (!s.setup_done) {
          router.replace("/setup");
        } else {
          setReady(true);
        }
      })
      .catch(() => setReady(true)); // backend unreachable — still show login
  }, [router]);

  return <div className="container">{ready && <LoginWizard />}</div>;
}
