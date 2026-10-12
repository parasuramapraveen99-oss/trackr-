"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { ApiError } from "@/lib/api-client";
import { CURRENT_TOS_VERSION } from "@/lib/terms";
import { AuthLayout, AuthError, authFormStyle } from "../components/AuthLayout";
import { Button, Checkbox, Field, Input, PasswordInput } from "../components/ds";

/**
 * Create an account.
 *
 * TWO THINGS CHANGED HERE, and they are the two smallest fixes with the largest effect
 * on a new user's first minute (docs/ONBOARDING_AS_IS_VS_TO_BE.md, screen 0).
 *
 * **The terms tick, which had no record anywhere.** There are two permissions in this
 * product and only one of them was ever stored. DPDP processing consent belongs to an
 * investor, is withdrawable, and is enforced on every ingestion path. Terms + Privacy is
 * a contract with the LOGIN — one per user, forever, not withdrawable — and it lived
 * nowhere at all. It is asked here because this is the moment the relationship starts,
 * and it is the ONE step in the whole flow that is not skippable. The version is sent
 * with the registration and stamped onto `app_user`, so what was accepted is recorded
 * rather than assumed.
 *
 * **Where this lands.** It used to push to `/settings` — the most technical screen in the
 * app, all investor codes and family group IDs, shown to someone who has not yet seen a
 * single number. Now it lands on the dashboard: empty on day one, but it is the product,
 * and the setup wizard opens over it.
 */
export default function RegisterPage() {
  const { register } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [acceptedTerms, setAcceptedTerms] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirmPassword) {
      setError("Passwords don't match");
      return;
    }
    // `required` on the input rejects an empty value but not a space, and a name that is
    // only whitespace would be stored and then shown back as a blank.
    if (!displayName.trim()) {
      setError("Please enter your name");
      return;
    }
    if (!acceptedTerms) {
      setError("Please accept the Terms and Privacy Policy to continue");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await register({
        email,
        password,
        display_name: displayName.trim(),
        tos_version: CURRENT_TOS_VERSION,
      });
      // The dashboard, not Settings. The wizard opens over it (components/Shell.tsx),
      // so the first screen is the thing they signed up for rather than a form asking
      // for a family group ID.
      router.push("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create account");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthLayout title="Create an account" subtitle="Trackr+ portfolio intelligence">
      <form onSubmit={onSubmit} style={authFormStyle}>
        <AuthError message={error} />
        <Field label="Name" htmlFor="name">
          <Input id="name" required autoComplete="name" value={displayName} onChange={setDisplayName} />
        </Field>
        <Field label="Email" htmlFor="email">
          <Input id="email" type="email" required autoComplete="email" value={email} onChange={setEmail} />
        </Field>
        <Field label="Password" hint="At least 8 characters." htmlFor="password">
          <PasswordInput id="password" required autoComplete="new-password" value={password} onChange={setPassword} />
        </Field>
        <Field label="Confirm password" htmlFor="confirm">
          <PasswordInput
            id="confirm"
            required
            autoComplete="new-password"
            value={confirmPassword}
            onChange={setConfirmPassword}
          />
        </Field>

        <Checkbox
          id="accept-terms"
          checked={acceptedTerms}
          onChange={setAcceptedTerms}
          style={{ alignItems: "flex-start", font: "var(--type-body-sm)" }}
          label={
            <span style={{ display: "block", lineHeight: 1.5 }}>
              I accept the{" "}
              <Link href="/terms" target="_blank">
                Terms of Service and Privacy Policy
              </Link>
              .
            </span>
          }
        />

        <Button type="submit" fullWidth disabled={submitting || !acceptedTerms}>
          {submitting ? "Creating…" : "Create account"}
        </Button>
      </form>

      <p style={{ margin: "14px 0 0", textAlign: "center", font: "var(--type-body)", color: "var(--text-tertiary)" }}>
        Already have an account? <Link href="/login">Sign in</Link>
      </p>
    </AuthLayout>
  );
}
