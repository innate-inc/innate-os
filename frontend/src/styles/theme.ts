import { createGlobalStyle } from "styled-components";

// Theme definition - Robotics Control Panel Design
export const theme = {
  colors: {
    background: "#000000",
    foreground: "#FFFFFF",
    primary: "#401FFB",
    primaryHover: "#5a3dfc",
    secondary: "#111111",
    muted: "rgba(255, 255, 255, 0.6)",
    error: "#ff3b3b",
    success: "#10b981",
    border: "#FFFFFF",
    cardBackground: "#111111",
    inputBackground: "#111111",
    buttonBackground: "#000000",
    panelBg: "#111111",
  },
  fonts: {
    body: "'DM Mono', monospace",
    heading: "'Space Grotesk', sans-serif",
    display: "'Space Grotesk', sans-serif",
    mono: "'DM Mono', monospace",
  },
  fontWeights: {
    light: 300,
    normal: 400,
    medium: 500,
    semibold: 600,
    bold: 700,
    extraBold: 800,
  },
  borderRadius: "0px",
  borderWidth: "1px",
  spacing: {
    unit: "16px",
  },
  shadows: {
    small: "4px 4px 0 rgba(255,255,255,0.05)",
    medium: "10px 10px 0 rgba(255,255,255,0.05)",
    large: "10px 10px 0 rgba(255,255,255,0.05)",
  },
};

// Global styles
export const GlobalStyle = createGlobalStyle`
  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
    -webkit-font-smoothing: antialiased;
  }

  body {
    font-family: ${theme.fonts.body};
    background-color: ${theme.colors.background};
    color: ${theme.colors.foreground};
    font-size: 14px;
    line-height: 1.4;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  #root {
    width: 100%;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }

  h1, h2, h3, h4, h5, h6 {
    font-family: ${theme.fonts.heading};
    margin: 0;
  }

  button {
    font-family: ${theme.fonts.mono};
    background-color: ${theme.colors.buttonBackground};
    color: ${theme.colors.foreground};
    border: 1px solid ${theme.colors.foreground};
    cursor: pointer;
    transition: all 0.2s;
  }

  button:hover {
    background: ${theme.colors.foreground};
    color: ${theme.colors.background};
  }

  button:focus,
  button:focus-visible {
    outline: none;
  }

  a {
    color: ${theme.colors.primary};
    text-decoration: none;
  }

  a:hover {
    color: ${theme.colors.primaryHover};
  }

  input, textarea, select {
    font-family: ${theme.fonts.mono};
    background-color: ${theme.colors.inputBackground};
    color: ${theme.colors.foreground};
    border: none;
    outline: none;
  }

  @keyframes pulse {
    0% { opacity: 1; }
    50% { opacity: 0.4; }
    100% { opacity: 1; }
  }

  @keyframes wave {
    0%, 100% { height: 10px; }
    50% { height: 35px; }
  }

  @keyframes gridMove {
    0% { background-position: 0 0; }
    100% { background-position: 50px 50px; }
  }
`;

export default theme;
