import React from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "styled-components";
import theme, { GlobalStyle } from "./styles/theme";
import "./index.css";
import App from "./App.tsx";
import { Auth0ProviderWithHistory } from "./auth/auth0-provider-with-history";

// Render the application
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider theme={theme}>
      <GlobalStyle />
      <Auth0ProviderWithHistory>
        <App />
      </Auth0ProviderWithHistory>
    </ThemeProvider>
  </React.StrictMode>
);
