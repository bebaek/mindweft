import { useMemo, useState, type PropsWithChildren } from "react";
import { MinigentApiClient, type Authentication } from "../api/client";
import { AuthContext } from "./auth-context";

export function AuthProvider({ children }: PropsWithChildren) {
  const [authentication, setAuthentication] = useState<Authentication>({ mode: "session" });
  const value = useMemo(
    () => ({
      authentication,
      api: new MinigentApiClient(authentication),
      setAuthentication,
    }),
    [authentication],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
