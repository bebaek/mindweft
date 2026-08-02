import { createContext, useContext } from "react";
import type {
  Authentication,
  MinigentApiClient,
  SessionPrincipal,
} from "../api/client";

export interface SessionState {
  loading: boolean;
  enabled: boolean;
  authenticated: boolean;
  principal: SessionPrincipal | null;
  error: string | null;
}

export interface AuthContextValue {
  authentication: Authentication;
  api: MinigentApiClient;
  session: SessionState;
  setAuthentication: (authentication: Authentication) => void;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshSession: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return value;
}
