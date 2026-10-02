const origin = 'https://deepseek.ac';
const urls = [
  '/ja/',
  '/ja/start/',
  '/ja/books/',
  '/ja/about/',
  '/ja/case-study/',
  '/ja/policy/',
];

export function GET() {
  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls
      .map((p) => `<url><loc>${origin}${p}</loc></url>`)
      .join('')}</urlset>`,
    { headers: { 'Content-Type': 'application/xml' } }
  );
}
