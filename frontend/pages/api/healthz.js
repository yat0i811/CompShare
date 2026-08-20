// 監視用のヘルスチェックエンドポイント。
// 認証不要・軽量・副作用なしであること（docs\APP_STANDARD.md の routes[].health.path 参照）。
//
// Next.js のページではなく API ルートにしているのは、
// ページだと React の描画とレイアウトを通るため、監視のたびに無駄な負荷がかかるため。
export default function handler(req, res) {
  res.status(200).json({ status: "ok" });
}
