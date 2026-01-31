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
    };
    fonts: {
      body: string;
      heading: string;
    };
    fontWeights: {
      normal: number;
      medium: number;
      semibold: number;
      bold: number;
    };
    borderRadius: string;
    shadows: {
      small: string;
      medium: string;
      large: string;
    };
  }
}
