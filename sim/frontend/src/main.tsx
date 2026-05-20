import ReactDOM from "react-dom/client";
import { ThemeProvider } from "styled-components";
import theme, { GlobalStyle } from "./styles/theme";
import "./index.css";
import App from "./App.tsx";

// Render the application
ReactDOM.createRoot(document.getElementById("root")!).render(
  <ThemeProvider theme={theme}>
    <GlobalStyle />
    <App />
  </ThemeProvider>
);
