// API URL Constants

// Function to check if running on localhost, only runs on the client side
export const isLocalhost = () => typeof window !== 'undefined' && (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1');

// 本番は同一ドメイン（compshare.yat0i.com）の nginx が NEXT_PUBLIC_BACKEND_BASE（既定 /be）配下で
// バックエンドへ中継するため、クライアントからは相対パスで呼び出す。
// ローカル（npm run dev でバックエンドを直接叩く場合）は従来どおり localhost:8001 に直接接続する。
const BACKEND_BASE = process.env.NEXT_PUBLIC_BACKEND_BASE || '/be';

export const BASE_URL = isLocalhost() ? 'http://localhost:8001' : BACKEND_BASE;

// WebSocket も同様に、現在のページのホストから相対的に組み立てる（nginx が /be/ws を中継する）。
// モジュール評価は Next.js のサーバー側バンドルでも走るため、window が無い環境では location を
// 参照しない（location はブラウザ専用のグローバルで、Node.js 上では参照するだけで例外になる）。
export const WS_URL_BASE = isLocalhost()
  ? 'ws://localhost:8001/ws'
  : (typeof window !== 'undefined'
      ? `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}${BACKEND_BASE}/ws`
      : `${BACKEND_BASE}/ws`);

export const GET_UPLOAD_URL_ENDPOINT = `${BASE_URL}/get-upload-url`;
export const COMPRESS_URL_ENDPOINT = `${BASE_URL}/compress/async/`;
export const DOWNLOAD_URL_ENDPOINT = `${BASE_URL}/download/`;
export const GET_DIRECT_DOWNLOAD_URL_ENDPOINT = `${BASE_URL}/get-download-url/`;
export const LOGIN_URL = `${BASE_URL}/auth/login`;
export const REGISTER_URL = `${BASE_URL}/auth/register`;
export const ME_URL = `${BASE_URL}/auth/me`;
export const CREATE_SHARE_URL = `${BASE_URL}/share/create`;
export const GET_SHARES_URL = `${BASE_URL}/shares`;
export const PUBLIC_DOWNLOAD_URL = `${BASE_URL}/share/`;

// Helper function to check token expiry
export const isTokenExpired = (token) => {
  if (!token) return true;
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    const expiry = payload.exp * 1000; // exp is in seconds, convert to milliseconds
    return Date.now() >= expiry;
  } catch (e) {
    console.error("Failed to decode or parse token:", e);
    return true; // Assume expired on error
  }
}; 