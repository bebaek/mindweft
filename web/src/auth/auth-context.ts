import { createContext, useContext } from "react";
import type { Authentication, MinigentApiClient } from "../api/client";

export interface AuthContextValue {
  authentication: Authentication;
  api: MinigentApiClient;
  setAuthentication: (authentication: Authentication) => void;
}

export const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return value;
}
