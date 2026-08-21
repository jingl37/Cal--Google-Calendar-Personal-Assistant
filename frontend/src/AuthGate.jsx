import { useEffect, useState } from "react";
import { Eye, EyeOff, LockKeyhole, Mail } from "lucide-react";
import { supabase } from "./supabase.js";
import Logo from "./Logo.jsx";
import "./AuthGate.css";

export default function AuthGate({ children }) {
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session);
      setLoading(false);
    });
    const { data: listener } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession);
      setLoading(false);
    });
    return () => listener.subscription.unsubscribe();
  }, []);

  async function submit(event) {
    event.preventDefault();
    setSubmitting(true);
    setMessage("");
    const result = mode === "signup"
      ? await supabase.auth.signUp({ email, password })
      : await supabase.auth.signInWithPassword({ email, password });
    if (result.error) setMessage(result.error.message);
    else if (mode === "signup" && !result.data.session) setMessage("Check your email to confirm your account, then sign in.");
    setSubmitting(false);
  }

  async function signInWithGoogle() {
    setSubmitting(true);
    setMessage("");
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: window.location.origin,
        queryParams: { access_type: "offline", prompt: "consent" },
      },
    });
    if (error) {
      setMessage(error.message);
      setSubmitting(false);
    }
  }

  async function resetPassword() {
    if (!email) return;
    setSubmitting(true);
    setMessage("");
    const { error } = await supabase.auth.resetPasswordForEmail(email, { redirectTo: window.location.origin });
    setMessage(error ? error.message : "Check your email for a password reset link.");
    setSubmitting(false);
  }

  function changeMode(nextMode) {
    setMode(nextMode);
    setMessage("");
  }

  if (loading) return <main className="auth-loading"><Logo size={54} active /></main>;
  if (session) return children;

  return <main className="auth-page">
    <section className="auth-card">
      <header className="auth-heading">
        <h1>{mode === "signup" ? "Create an account" : "Sign in"}</h1>
        <p>
          {mode === "signup" ? "Already have an account? " : "New user? "}
          <button type="button" onClick={() => changeMode(mode === "signup" ? "signin" : "signup")}>
            {mode === "signup" ? "Sign in" : "Create an account"}
          </button>
        </p>
      </header>

      <form onSubmit={submit}>
        <div className="auth-input">
          <Mail size={19} aria-hidden="true" />
          <input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" placeholder="Email Address" aria-label="Email Address" />
        </div>
        <div className="auth-input">
          <LockKeyhole size={19} aria-hidden="true" />
          <input required minLength={8} type={showPassword ? "text" : "password"} value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "signup" ? "new-password" : "current-password"} placeholder="Password" aria-label="Password" />
          <button className="password-toggle" type="button" onClick={() => setShowPassword(!showPassword)} aria-label={showPassword ? "Hide password" : "Show password"}>
            {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
          </button>
        </div>
        {mode === "signin" && <button className="forgot-password" type="button" onClick={resetPassword}>Forgot password?</button>}
        {message && <p className="auth-message">{message}</p>}
        <button className="auth-submit" disabled={submitting}>{submitting ? "Please wait…" : mode === "signup" ? "Create an account" : "Login"}</button>
      </form>

      <div className="auth-divider"><span>or</span></div>
      <div className="social-options">
        <button className="google-button" type="button" onClick={signInWithGoogle} disabled={submitting} aria-label="Continue with Google">
          <svg className="google-mark" viewBox="0 0 18 18" aria-hidden="true">
            <path fill="#4285F4" d="M17.64 9.205c0-.639-.057-1.252-.164-1.841H9v3.482h4.844a4.14 4.14 0 0 1-1.797 2.716v2.258h2.909c1.702-1.567 2.684-3.874 2.684-6.615Z" />
            <path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.909-2.258c-.806.54-1.835.859-3.047.859-2.344 0-4.329-1.585-5.037-3.711H.956v2.333A9 9 0 0 0 9 18Z" />
            <path fill="#FBBC05" d="M3.963 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.281-1.71V4.957H.956A9 9 0 0 0 0 9c0 1.452.347 2.827.956 4.043l3.007-2.333Z" />
            <path fill="#EA4335" d="M9 3.579c1.322 0 2.508.454 3.441 1.346l2.581-2.581C13.463.892 11.426 0 9 0A9 9 0 0 0 .956 4.957L3.963 7.29C4.671 5.164 6.656 3.579 9 3.579Z" />
          </svg>
        </button>
      </div>

      <p className="auth-legal">
        By signing in with an account, you agree to our<br />
        <a href="#terms">Terms of Service</a> and <a href="#privacy">Privacy Policy.</a>
      </p>
    </section>
  </main>;
}
