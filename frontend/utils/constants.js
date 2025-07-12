// API URL Constants

// Function to check if running on localhost, only runs on the client side
export const isLocalhost = () => typeof window !== 'undefined' && (window.location.hostname === 'localhost' || window.location.hostname === '120.0.0.1');

export const BASE_URL = isLocalhost() ? 'http://localhost:8001' : 'https://compshareapi.yat0i.com';
export const WS_URL_BASE = isLocalhost() ? 'ws://localhost:8001/ws' : 'wss://compshareapi.yat0i.com/ws';

export const GET_UPLOAD_URL_ENDPOINT = `${BASE_URL}/get-upload-url`;
export const COMPRESS_URL_ENDPOINT = `${BASE_URL}/compress/async/`;
export const DOWNLOAD_URL_ENDPOINT = `${BASE_URL}/download/`;
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