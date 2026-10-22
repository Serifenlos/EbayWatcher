// PostCSS-Konfiguration für CodeKit.
// CodeKit liest diese Datei, weil sie im Projekt-Hauptordner liegt (Name und Ort sind fest vorgegeben).
// Tailwind übersetzt damit source/app/static/css/app.css in die fertige CSS-Datei im build-Ordner.
export default {
  plugins: {
    "@tailwindcss/postcss": {
      // Ergebnis immer verkleinern, unabhängig von CodeKits Einstellungen
      optimize: { minify: true },
    },
  },
};
