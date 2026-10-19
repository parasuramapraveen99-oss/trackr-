"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { ApiError } from "@/lib/api-client";
import { AuthLayout, AuthError, authFormStyle } from "../components/AuthLayout";
import { Button, Field, Input, PasswordInput } from "../components/ds";

export default function LoginPage() {
  const { login, sessionError, retrySession, ready } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login({ email, password });
      router.push("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthLayout title="Trackr+" subtitle="Sign in to your portfolio">
      <form onSubmit={onSubmit} style={authFormStyle}>
        {sessionError && <div><AuthError message={sessionError} /><Button type="button" variant="ghost" disabled={!ready} onClick={() => void retrySession()}>Try connection again</Button></div>}
        <AuthError message={error} />
        <Field label="Email" htmlFor="email">
          <Input id="email" type="email" required autoComplete="email" value={email} onChange={setEmail} />
        </Field>
        <Field label="Password" htmlFor="password">
          <PasswordInput
            id="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={setPassword}
          />
        </Field>
        <Button type="submit" fullWidth disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </Button>
      </form>

      <p style={{ margin: "14px 0 0", textAlign: "center", font: "var(--type-body)", color: "var(--text-tertiary)" }}>
        New here? <Link href="/register">Create an account</Link>
      </p>
    </AuthLayout>
  );
}
