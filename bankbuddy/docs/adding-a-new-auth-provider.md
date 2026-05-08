# Adding a new auth provider

Every identity provider (Entra, Auth0, Cognito, Keycloak, Okta, ...) plugs in via the `IAuthProvider` interface.

## Steps

1. **Create the adapter**

   ```text
   services/api/app/auth/<name>_provider.py
   ```

   Inherit from `bankbuddy_shared.interfaces.IAuthProvider` and implement:
   - `get_login_url(state)` -> the IdP authorize URL
   - `exchange_code(code)` -> verified `Principal`
   - `verify_token(token)` -> verified `Principal`

   Use a vetted OIDC library (`msal` for Entra, `authlib` for generic OIDC).

2. **Register in the factory**

   In `services/api/app/auth/factory.py` (Phase 1d), add a branch and pull required env vars from settings.

3. **Add env vars**

   Update `.env.example` with the new provider's settings (issuer URL, client id, client secret, redirect URI). Mark secrets as required and load them through `ISecretProvider` in production.

4. **UI awareness (optional)**

   The UI reads `AUTH_PROVIDER` from `/config`. If your provider needs a different login button label or flow, branch on that value.

5. **Switch via env**

   ```ini
   AUTH_PROVIDER=<name>
   ```

   Restart the api container. UI and agent are unchanged.

## Rules

- **No vendor SDK imports outside the adapter file.**
- **Always verify token signature and issuer.** Never accept unsigned tokens.
- **Map errors to `AuthError`.** Return 401 from the API, not a vendor-specific status.
- **Don't store IdP tokens in the browser.** The UI only sees the app-issued JWT.
