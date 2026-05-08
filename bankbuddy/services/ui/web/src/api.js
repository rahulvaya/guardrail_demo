// Tiny API client. Holds the api base URL (read from /config at runtime so
// the same React bundle works against any backend).
let _baseUrl = '';

async function _json(method, path, body) {
  const res = await fetch(`${_baseUrl}${path}`, {
    method,
    credentials: 'include',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${text}`);
  }
  return res.json();
}

export const api = {
  setBaseUrl(url) { _baseUrl = url.replace(/\/$/, ''); },
  me: () => _json('GET', '/me'),
  logout: () => _json('POST', '/auth/logout'),
  loginUrl: () => _json('GET', '/auth/login-url'),
  localDevExchange: (username) => _json('POST', '/auth/local-dev/exchange', { username }),
  chat: (message, sessionId) => _json('POST', '/chat', { message, session_id: sessionId }),
};
