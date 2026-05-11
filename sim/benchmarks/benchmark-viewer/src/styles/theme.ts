export const theme = {
  colors: {
    success: '#4CAF50',
    error: '#FF5252',
    background: '#f5f7fa',
    surface: '#ffffff',
    text: '#2c3e50',
    textLight: '#546e7a',
    border: '#e0e4e8',
  },
  shadows: {
    card: '0 2px 4px rgba(0,0,0,0.1)',
    modal: '0 4px 12px rgba(0,0,0,0.15)',
  },
  borderRadius: {
    small: '4px',
    medium: '8px',
    large: '12px',
  },
  spacing: {
    xs: '0.25rem',
    sm: '0.5rem',
    md: '1rem',
    lg: '1.5rem',
    xl: '2rem',
  },
} as const;

export type Theme = typeof theme;