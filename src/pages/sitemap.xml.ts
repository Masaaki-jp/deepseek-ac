import { getCollection } from 'astro:content';

const origin = 'https://deepseek.ac';
const fixedUrls = [
  '/ja/',
  '/ja/start/',
  '/ja/articles/',
  '/ja/books/',
  '/ja/about/',
  '/ja/case-study/',
  '/ja/policy/',
];

export async function GET() {
  const articles = await getCollection('articles', ({ data }) => !data.draft);
  const articleUrls = articles.map((a) => `/ja/articles/${a.id}/`);
  const urls = [...fixedUrls, ...articleUrls];

  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls
      .map((p) => `<url><loc>${origin}${p}</loc></url>`)
      .join('')}</urlset>`,
    { headers: { 'Content-Type': 'application/xml' } }
  );
}
