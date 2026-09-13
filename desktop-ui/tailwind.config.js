/** Boombiz Guard setup UI — black, white and yellow, on the Boombiz POS
 *  system: Inter, square corners, hairline borders. Yellow is a FILL carrying
 *  black text; it is never a word on white. Mirrors `brand.guard` in the main
 *  Boombiz tailwind config so the two stay one brand. */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        guard: {
          50: "#FFF9E6",
          100: "#FFEFB8",
          400: "#FFD24D",
          500: "#FFC21A",
          600: "#E6A800",
          700: "#8A6400",
          ink: "#0B0B0B",
          night: "#141414",
        },
      },
      fontFamily: {
        sans: ["Inter", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "sans-serif"],
      },
      borderRadius: { none: "0", sm: "0", DEFAULT: "0", md: "0", lg: "0", xl: "0", "2xl": "0", full: "9999px" },
    },
  },
  plugins: [],
};
