const origin = 'https://deepseek.ac';

export function GET() {
  return new Response(
    `# deepseek.ac\n\n日本語の独立したDeepSeek教育サイト。\n取得可能であることは二次利用への許諾ではありません。利用条件: ${origin}/ja/policy/\n`
  );
}
