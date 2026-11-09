/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        stark: {
          gold: "#CFAC57",
          gray: "#E0E0E0",
          black: "#000000",
          white: "#FFFFFF",
          ink: "#141414",
        },
      },
      fontFamily: {
        sans: ["Inter", "Helvetica Neue", "Arial", "sans-serif"],
      },
      keyframes: {
        indeterminate: {
          "0%": { transform: "translateX(0%)" },
          "100%": { transform: "translateX(150%)" },
        },
      },
      animation: {
        indeterminate: "indeterminate 1.2s ease-in-out infinite alternate",
      },
    },
  },
  plugins: [],
};
