import { useEffect, useState } from 'react';
import { api } from './api';
import Login from './Login';
import Chat from './Chat';

export default function App() {
  const [config, setConfig] = useState(null);
  const [me, setMe] = useState(null);
  const [bootError, setBootError] = useState(null);

  useEffect(() => {
    fetch('/config')
      .then((r) => r.json())
      .then(setConfig)
      .catch((e) => setBootError(`failed to load /config: ${e}`));
  }, []);

  useEffect(() => {
    if (!config?.api_base_url) return;
    api.setBaseUrl(config.api_base_url);
    api.me().then(setMe).catch(() => setMe(null));
  }, [config]);

  if (bootError) return <main className="container"><p className="error">{bootError}</p></main>;
  if (!config) return <main className="container"><p>loading...</p></main>;

  return (
    <main className="container">
      <header className="topbar">
        <h1>BankBuddy</h1>
        <div className="meta">
          <span className="provider">auth: {config.auth_provider}</span>
          {me && (
            <>
              <span className="user">signed in as {me.username}</span>
              <button className="link" onClick={async () => { await api.logout(); setMe(null); }}>
                sign out
              </button>
            </>
          )}
        </div>
      </header>
      {!me ? (
        <Login provider={config.auth_provider} onLoggedIn={setMe} />
      ) : (
        <Chat me={me} />
      )}
    </main>
  );
}
