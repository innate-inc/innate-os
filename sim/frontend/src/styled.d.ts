import "styled-components";

declare module "styled-components" {
  export interface DefaultTheme {
    colors: {
      background: string;
      foreground: string;
      primary: string;
      primaryHover: string;
      secondary: string;
      muted: string;
      error: string;
      success: string;
      border: string;
      cardBackground: string;
      inputBackground: string;
      buttonBackground: string;
      panelBg: string;
    };
    fonts: {
      body: string;
      heading: string;
      display: string;
      mono: string;
    };
    fontWeights: {
      light: number;
      normal: number;
      medium: number;
      semibold: number;
      bold: number;
      extraBold: number;
    };
    borderRadius: string;
    borderWidth: string;
    spacing: {
      unit: string;
    };
    shadows: {
      small: string;
      medium: string;
      large: string;
    };
  }
}
