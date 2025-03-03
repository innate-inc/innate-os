import { createGlobalStyle } from "styled-components";

// Theme definition
export const theme = {
  colors: {
    background: "#0f172a",
    foreground: "rgba(255, 255, 255, 0.87)",
    primary: "#646cff",
    primaryHover: "#535bf2",
    secondary: "#1e293b",
    muted: "#a1a1aa",
    error: "#ef4444",
    success: "#10b981",
    border: "#334155",
    cardBackground: "#1e293b",
    inputBackground: "#1a1a1a",
    buttonBackground: "#1a1a1a",
  },
  fonts: {
    body: "'Inter', system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
    heading:
      "'Inter', system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
  },
  fontWeights: {
    normal: 400,
    medium: 500,
    semibold: 600,
    bold: 700,
  },
  borderRadius: "8px",
  shadows: {
    small: "0 1px 2px rgba(0, 0, 0, 0.05)",
    medium:
      "0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06)",
    large:
      "0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05)",
  },
};

// Global styles
export const GlobalStyle = createGlobalStyle`
  body {
    font-family: ${theme.fonts.body};
    background-color: ${theme.colors.background};
    color: ${theme.colors.foreground};
    margin: 0;
    padding: 0;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
  }

  h1, h2, h3, h4, h5, h6 {
    font-family: ${theme.fonts.heading};
    margin: 0;
  }

  button {
    font-family: ${theme.fonts.body};
    background-color: ${theme.colors.buttonBackground};
    color: ${theme.colors.foreground};
    border: 1px solid transparent;
    border-radius: ${theme.borderRadius};
    cursor: pointer;
    transition: border-color 0.25s, background-color 0.25s;
  }

  button:hover {
    border-color: ${theme.colors.primary};
  }

  button:focus,
  button:focus-visible {
    outline: 4px auto -webkit-focus-ring-color;
  }

  a {
    color: ${theme.colors.primary};
    text-decoration: none;
  }

  a:hover {
    color: ${theme.colors.primaryHover};
  }

  input, textarea, select {
    font-family: ${theme.fonts.body};
    background-color: ${theme.colors.inputBackground};
    color: ${theme.colors.foreground};
    border: 1px solid ${theme.colors.border};
    border-radius: ${theme.borderRadius};
  }
`;

export default theme;
