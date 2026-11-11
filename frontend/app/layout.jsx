import "./globals.css";

export const metadata = {
  title: "Export Proceeds Realization",
  description: "Review extracted documents and fill them into the ERP",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
