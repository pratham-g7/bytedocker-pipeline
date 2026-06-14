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
      // Restrained depth (Linear-leaning): cool slate-tinted, low-spread.
      boxShadow: {
        xs: "0 1px 2px 0 rgb(15 23 42 / 0.04), 0 1px 1px -1px rgb(15 23 42 / 0.03)",
        card: "0 6px 20px -6px rgb(15 23 42 / 0.12), 0 2px 6px -2px rgb(15 23 42 / 0.06)",
        pop: "0 16px 44px -12px rgb(15 23 42 / 0.24), 0 4px 12px -4px rgb(15 23 42 / 0.10)",
      },
      transitionTimingFunction: {
        premium: "cubic-bezier(0.16, 1, 0.3, 1)",
      },
    },
  },
  plugins: [],
};
