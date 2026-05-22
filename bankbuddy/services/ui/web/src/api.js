// Tiny API client. Holds the api base URL (read from /config at runtime so
// the same React bundle works against any backend).
let _baseUrl = '';
let _provider = null;
let _lastLocalUsername = null;
const LS_KEY = 'bb_last_local_user';
try { _lastLocalUsername = localStorage.getItem(LS_KEY); } catch (_) { /* ignore */ }

// Endpoints that must NEVER trigger silent re-auth on 401 (avoid infinite loops).
const AUTH_PATHS = new Set(['/me', '/auth/login-url', '/auth/local-dev/exchange', '/auth/logout']);

async function _rawFetch(method, path, body) {
  return fetch(`${_baseUrl}${path}`, {
    method,
    credentials: 'include',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
}

async function _silentReauth() {
  if (_provider === 'local-dev' && _lastLocalUsername) {
    const r = await _rawFetch('POST', '/auth/local-dev/exchange', { username: _lastLocalUsername });
    return r.ok;
  }
  // Hosted IdP (e.g. AAD): full-page redirect through /auth/login-url.
  try {
    const r = await _rawFetch('GET', '/auth/login-url');
    if (r.ok) {
      const { login_url } = await r.json();
      if (login_url) { window.location.href = login_url; return false; }
    }
  } catch (_) { /* ignore */ }
  return false;
}

async function _json(method, path, body) {
  let res = await _rawFetch(method, path, body);
  if (res.status === 401 && !AUTH_PATHS.has(path)) {
    const ok = await _silentReauth();
    if (ok) res = await _rawFetch(method, path, body);
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  return res.json();
}

export const api = {
  setBaseUrl(url) { _baseUrl = url.replace(/\/$/, ''); },
  setProvider(p) { _provider = p; },
  me: () => _json('GET', '/me'),
  logout: () => _json('POST', '/auth/logout'),
  loginUrl: () => _json('GET', '/auth/login-url'),
  localDevExchange: async (username) => {
    const r = await _json('POST', '/auth/local-dev/exchange', { username });
    _lastLocalUsername = username;
    try { localStorage.setItem(LS_KEY, username); } catch (_) { /* ignore */ }
    return r;
  },
  chat: (message, sessionId) => _json('POST', '/chat', { message, session_id: sessionId }),
};
