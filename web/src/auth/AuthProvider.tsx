import { useCallback, useEffect, useMemo, useState, type PropsWithChildren } from "react";
import { ApiError, MinigentApiClient, type Authentication } from "../api/client";
import { AuthContext, type SessionState } from "./auth-context";

const initialSession: SessionState = {
  loading: true,
  enabled: false,
  authenticated: false,
  principal: null,
  error: null,
};

export function AuthProvider({ children }: PropsWithChildren) {
  const [authentication, setAuthentication] = useState<Authentication>({ mode: "session" });
  const [session, setSession] = useState<SessionState>(initialSession);
  const api = useMemo(() => new MinigentApiClient(authentication), [authentication]);

  const refreshSession = useCallback(async () => {
    setSession((current) => ({
      ...current,
      loading: current.authenticated ? false : true,
      error: null,
    }));
    try {
      const status = await new MinigentApiClient({ mode: "session" }).getSession();
      setSession({
        loading: false,
        enabled: status.enabled,
        authenticated: status.authenticated,
        principal: status.principal ?? null,
        error: null,
      });
    } catch (error) {
      setSession({
        loading: false,
        enabled: false,
        authenticated: false,
        principal: null,
        error: errorMessage(error),
      });
    }
  }, []);

  useEffect(() => {
    let active = true;
    void new MinigentApiClient({ mode: "session" }).getSession().then(
      (status) => {
        if (!active) return;
        setSession({
          loading: false,
          enabled: status.enabled,
          authenticated: status.authenticated,
          principal: status.principal ?? null,
          error: null,
        });
      },
      (error: unknown) => {
        if (!active) return;
        setSession({
          loading: false,
          enabled: false,
          authenticated: false,
          principal: null,
          error: errorMessage(error),
        });
      },
    );
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    // Refresh the server-side session cookie while the console is open. The
    // endpoint only renews tokens close to expiry, so this does not extend an
    // idle session indefinitely.
    const interval = window.setInterval(() => {
      void refreshSession();
    }, 5 * 60 * 1000);
    return () => window.clearInterval(interval);
  }, [refreshSession]);

  const login = useCallback(async (username: string, password: string) => {
    const status = await new MinigentApiClient({ mode: "session" }).login(username, password);
    setSession({
      loading: false,
      enabled: status.enabled,
      authenticated: status.authenticated,
      principal: status.principal ?? null,
      error: null,
    });
    setAuthentication({ mode: "session" });
  }, []);

  const completePasswordSetup = useCallback(async (token: string, password: string) => {
    const status = await new MinigentApiClient({ mode: "session" }).completePasswordSetup(token, password);
    setSession({
      loading: false,
      enabled: status.enabled,
      authenticated: status.authenticated,
      principal: status.principal ?? null,
      error: null,
    });
    setAuthentication({ mode: "session" });
  }, []);

  const logout = useCallback(async () => {
    await new MinigentApiClient({ mode: "session" }).logout();
    setSession((current) => ({
      ...current,
      authenticated: false,
      principal: null,
      error: null,
    }));
    setAuthentication({ mode: "session" });
  }, []);

  const value = useMemo(
    () => ({
      authentication,
      api,
      session,
      setAuthentication,
      login,
      completePasswordSetup,
      logout,
      refreshSession,
    }),
    [api, authentication, completePasswordSetup, login, logout, refreshSession, session],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return "Unable to check the authentication session.";
}
