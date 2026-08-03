// Sign-in against Google Identity Platform, over its REST API.
//
// No SDK and no CDN script: email/password needs three endpoints, and
// talking to them directly keeps this the same plain-JS frontend as the
// rest of the app - with nothing third-party running on the page.
//
// Passwords are sent to Identity Platform and never to this application,
// which is the point of using it: hashing, reset and rate limiting are
// its problem, not ours.

const Auth = (() => {
  const STORE_KEY = "guru.auth";
  const IDENTITY = "https://identitytoolkit.googleapis.com/v1/accounts";
  const SECURETOKEN = "https://securetoken.googleapis.com/v1/token";

  let config = null;
  let session = null; // { idToken, refreshToken, expiresAt, email, localId }
  const listeners = [];

  function load() {
    try {
      session = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    } catch (_) {
      session = null;
    }
  }

  function save() {
    if (session) localStorage.setItem(STORE_KEY, JSON.stringify(session));
    else localStorage.removeItem(STORE_KEY);
    for (const fn of listeners) fn(session);
  }

  function setSession(data) {
    session = {
      idToken: data.idToken,
      refreshToken: data.refreshToken,
      // Refresh a minute early rather than on the failure it would
      // otherwise cause mid-request.
      expiresAt: Date.now() + (Number(data.expiresIn || 3600) - 60) * 1000,
      email: data.email,
      localId: data.localId || data.user_id,
    };
    save();
  }

  async function call(url, body) {
    const res = await fetch(`${url}?key=${config.api_key}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      // Identity Platform returns machine codes; turn the common ones
      // into something a student can act on.
      const code = (payload.error && payload.error.message) || "REQUEST_FAILED";
      throw new Error(readable(code));
    }
    return payload;
  }

  function readable(code) {
    const map = {
      EMAIL_EXISTS: "That email already has an account. Try signing in instead.",
      EMAIL_NOT_FOUND: "No account with that email.",
      INVALID_PASSWORD: "That password is not right.",
      INVALID_LOGIN_CREDENTIALS: "Email or password is not right.",
      WEAK_PASSWORD: "Password must be at least 6 characters.",
      INVALID_EMAIL: "That does not look like an email address.",
      TOO_MANY_ATTEMPTS_TRY_LATER: "Too many attempts. Wait a moment and try again.",
      USER_DISABLED: "This account has been disabled.",
    };
    return map[code.split(" : ")[0]] || code;
  }

  return {
    async init() {
      load();
      config = await fetch("/api/config").then((r) => r.json());
      return config;
    },

    config: () => config,
    session: () => session,
    signedIn: () => Boolean(session) || (config && config.auth_disabled),
    onChange(fn) {
      listeners.push(fn);
    },

    async signUp(email, password) {
      setSession(await call(`${IDENTITY}:signUp`, { email, password, returnSecureToken: true }));
    },

    async signIn(email, password) {
      setSession(
        await call(`${IDENTITY}:signInWithPassword`, { email, password, returnSecureToken: true })
      );
    },

    signOut() {
      session = null;
      save();
    },

    // A valid ID token, refreshing it first if it is about to expire.
    // Every API call goes through this, so a session that outlives the
    // one-hour token lifetime keeps working without the student noticing.
    async token() {
      if (config && config.auth_disabled) return null;
      if (!session) return null;

      if (Date.now() >= session.expiresAt) {
        const res = await fetch(`${SECURETOKEN}?key=${config.api_key}`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            grant_type: "refresh_token",
            refresh_token: session.refreshToken,
          }),
        });
        if (!res.ok) {
          // The refresh token is gone or revoked; make them sign in again
          // rather than failing every request from here on.
          this.signOut();
          return null;
        }
        const data = await res.json();
        session.idToken = data.id_token;
        session.refreshToken = data.refresh_token;
        session.expiresAt = Date.now() + (Number(data.expires_in || 3600) - 60) * 1000;
        save();
      }
      return session.idToken;
    },

    async authHeaders() {
      const token = await this.token();
      return token ? { Authorization: `Bearer ${token}` } : {};
    },
  };
})();
