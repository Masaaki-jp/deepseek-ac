import { getCollection } from 'astro:content';

const origin = 'https://deepseek.ac';

export async function GET() {
  const articles = (await getCollection('articles', ({ data }) => !data.draft))
    .sort((a, b) => b.data.publishedAt.getTime() - a.data.publishedAt.getTime());

  const list = articles
    .map((a) => `- [${a.data.title}](${origin}/ja/articles/${a.id}/): ${a.data.description}`)
    .join('\n') || '公開中の記事はまだありません。';

  return new Response(
    `# deepseek.ac\n\n日本語の独立したDeepSeek教育サイト。\n取得可能であることは二次利用への許諾ではありません。利用条件: ${origin}/ja/policy/\n\n## 記事\n${list}\n`
  );
}
