import { Html, Head, Main, NextScript } from 'next/document';

export default function Document() {
  return (
    <Html lang="ja">
      <Head>
        {/* テーマのちらつき（FOUC）防止。CSS 読み込みより前に、必ずインラインで置くこと。
            外部ファイルにすると読み込み待ちの間に既定テーマが描画されてしまう。
            Next.js では _app.js では遅いため、_document.js の <Head> に置く。
            docs\APP_STANDARD.md §9-3 参照。 */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function () {
                try {
                  var t = localStorage.getItem('theme');
                  document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light');
                } catch (e) {
                  document.documentElement.setAttribute('data-theme', 'light');
                }
              })();
            `,
          }}
        />
      </Head>
      <body>
        <Main />
        <NextScript />
      </body>
    </Html>
  );
}
