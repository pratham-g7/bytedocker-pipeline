/** @type {import('tailwindcss').Config} */
module.exports = {
  // templatetags emit class names (e.g. nav-active) that never appear in HTML source
  content: ["./templates/**/*.html", "./pipeline/templatetags/*.py"],
  theme: {
    extend: {
      colors: {
        // Brand: near-black ink on paper, one electric-cobalt accent.
        ink: "#0A0A0A",
        accent: {
          DEFAULT: "#2447F0",
          50: "#EEF1FE",
          100: "#DCE3FD",
          600: "#2447F0",
          700: "#1D39C9",
          800: "#182E9E",
        },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
        display: ["Montserrat", "Inter", "ui-sans-serif", "sans-serif"],
      },
    },
  },
  plugins: [],
};
