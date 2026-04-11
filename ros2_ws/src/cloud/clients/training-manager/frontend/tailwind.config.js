/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        innate: {
          purple: "#401ffb",
          "purple-hover": "#7569fd",
          orange: "#FB601F",
          border: "#f1eeee",
          muted: "#999",
          panel: "#faf9f9",
        },
      },
      fontFamily: {
        sans: ["system-ui", "-apple-system", "sans-serif"],
        mono: ["ui-monospace", "'SF Mono'", "monospace"],
      },
    },
  },
  plugins: [],
};
