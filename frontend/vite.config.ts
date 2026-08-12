import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// https://vite.dev/config/
export default defineConfig({
  // viteSingleFile precisa vir depois do react()/tailwindcss(): inlina JS/CSS/assets
  // (base64) num único dist/index.html -- é o que permite o Streamlit embutir o
  // build via st.components.v1.html sem precisar servir arquivos estáticos
  // separados (ver src/dashboard/react_embed.py).
  plugins: [react(), tailwindcss(), viteSingleFile()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
});
