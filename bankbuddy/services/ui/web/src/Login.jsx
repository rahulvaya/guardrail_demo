import { useState } from 'react';
import { api } from './api';

export default function Login({ provider, onLoggedIn }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  async function pick(username) {
    setBusy(true);
    setErr(null);
    try {
      const me = await api.localDevExchange(username);
      onLoggedIn(me);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function ssoRedirect() {
    setBusy(true);
    try {
      const { login_url } = await api.loginUrl();
      window.location.href = login_url;
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  }

  return (
    <section className="card login">
      <h2>Sign in</h2>
      {provider === 'local-dev' ? (
        <>
          <p className="hint">Local dev mode - pick a demo customer:</p>
          <div className="btn-row">
            <button disabled={busy} onClick={() => pick('alice')}>Alice (760 credit)</button>
            <button disabled={busy} onClick={() => pick('bob')}>Bob (640 credit)</button>
          </div>
        </>
      ) : (
        <>
          <p className="hint">You will be redirected to your identity provider.</p>
          <button disabled={busy} onClick={ssoRedirect}>Continue with {provider}</button>
        </>
      )}
      {err && <p className="error">{err}</p>}
    </section>
  );
}
