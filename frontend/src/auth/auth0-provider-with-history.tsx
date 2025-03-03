import { Auth0Provider } from "@auth0/auth0-react";
import { ReactNode, useEffect } from "react";

interface Auth0ProviderWithHistoryProps {
  children: ReactNode;
}

export const Auth0ProviderWithHistory = ({
  children,
}: Auth0ProviderWithHistoryProps) => {
  const domain = import.meta.env.VITE_AUTH0_DOMAIN;
  const clientId = import.meta.env.VITE_AUTH0_CLIENT_ID;

  const redirectUri = window.location.origin;

  // Check if we should skip the redirect callback
  const shouldSkipRedirectCallback = () => {
    const query = window.location.search;
    return !(query.includes("code=") && query.includes("state="));
  };

  useEffect(() => {
    // Log detailed Auth0 configuration for debugging
    console.log("Auth0 detailed config:", {
      domain,
      clientId,
      redirectUri,
      origin: window.location.origin,
      pathname: window.location.pathname,
      href: window.location.href,
      shouldSkipRedirect: shouldSkipRedirectCallback(),
    });
  }, []);

  if (!domain || !clientId) {
    console.error(
      "Auth0 domain or client ID is missing. Authentication will not work."
    );
    return <>{children}</>;
  }

  // Define authorization parameters
  const authorizationParams = {
    redirect_uri: redirectUri,
  };

  console.log("Using authorization params:", authorizationParams);

  return (
    <Auth0Provider
      domain={domain}
      clientId={clientId}
      authorizationParams={authorizationParams}
      //   cacheLocation="localstorage"
      //   useRefreshTokens={true}
      //   skipRedirectCallback={shouldSkipRedirectCallback()}
      //   onRedirectCallback={(appState) => {
      //     console.log("Auth0 redirect callback", appState);
      //     window.history.replaceState(
      //       {},
      //       document.title,
      //       appState?.returnTo || window.location.pathname
      //     );
      //   }}
    >
      {children}
    </Auth0Provider>
  );
};
