// src/context/AuthContext.js
import React, { createContext, useState, useEffect, useRef } from 'react';
import { LOGIN_URL, ME_URL, isTokenExpired } from '../utils/constants';

// AuthContext を作成
export const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [token, setToken] = useState(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [userInfo, setUserInfo] = useState(null);
  // userInfo の取得がリトライを使い切って失敗したかどうか。
  // 「取得中」と「取得失敗」を区別できないと、失敗時に画面が永久に読み込み中のままになる。
  const [userInfoFetchFailed, setUserInfoFetchFailed] = useState(false);

  // ログアウト処理
  const handleLogout = () => {
    if (typeof window !== 'undefined') {
      localStorage.removeItem('authToken');
    }
    setToken(null);
    setIsAdmin(false);
    setUserInfo(null);
    setUserInfoFetchFailed(false);
    setUsername('');
    setPassword('');
  };

  // ログイン処理
  const handleLogin = async () => {
    const formData = new FormData();
    formData.append('username', username);
    formData.append('password', password);

    try {
      const res = await fetch(LOGIN_URL, { method: 'POST', body: formData });
      if (res.ok) {
        const data = await res.json();
        setToken(data.token);
        if (typeof window !== 'undefined') {
          localStorage.setItem('authToken', data.token);
        }
      } else {
        const err = await res.json().catch(() => ({ detail: 'ログインに失敗しました。' }));
        alert(err.detail || 'ログインに失敗しました。');
      }
    } catch (error) {
      console.error('Login error:', error);
      alert('ログインリクエスト中にエラーが発生しました。');
    }
  };

  // トークンが localStorage に残っていれば読み込み
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const savedToken = localStorage.getItem('authToken');
    if (savedToken && !isTokenExpired(savedToken)) {
      setToken(savedToken);
    } else {
      handleLogout();
    }
  }, []);

  // token が変わるたびに isAdmin を判定し、かつユーザー情報を取得
  useEffect(() => {
    if (!token) {
      setIsAdmin(false);
      setUserInfo(null);
      return;
    }
    try {
      const payload = JSON.parse(atob(token.split('.')[1]));
      if (payload && payload.is_admin) {
        setIsAdmin(true);
      } else {
        setIsAdmin(false);
      }
      // /auth/me エンドポイントからユーザー情報を取得
      fetchUserInfo(token);
    } catch (error) {
      console.error('Token decode error in AuthProvider:', error);
      setIsAdmin(false);
      setUserInfo(null);
    }
  }, [token]);

  // トークンの有効期限を定期的にチェックして、自動ログアウト
  useEffect(() => {
    if (!token) return;
    const interval = setInterval(() => {
      if (isTokenExpired(token)) {
        alert('セッションが切れました。再ログインしてください。');
        handleLogout();
      }
    }, 60 * 1000);
    return () => clearInterval(interval);
  }, [token]);

  // /auth/me から現在のユーザー情報を取得し、userInfo をセットする。
  // 取得できた userInfo を返し、失敗した場合は null を返す。
  // この関数は useEffect([token]) から1回しか呼ばれないため、一度失敗すると
  // userInfo が null のままになりアップロードが恒久的にブロックされる。
  // それを防ぐために軽いバックオフ付きで数回リトライする。
  const RETRY_DELAYS_MS = [500, 1500]; // 初回 + 2回リトライ = 最大3回

  // 実行中の /auth/me リクエストを保持する。
  // fetchUserInfo は useEffect([token]) と refreshUserInfo の2経路から呼ばれるため、
  // ガードが無いと初回のバックオフ待機（最大2秒）中にアップロードボタンが押された場合に
  // 2本目が並走する。後発の結果が先発の userInfoFetchFailed を上書きしたり、
  // 片方の 401 で handleLogout が走る競合を防ぐため、実行中は同じ Promise を共有する。
  const inFlightFetchRef = useRef(null);

  const fetchUserInfo = async (currentToken) => {
    if (!currentToken) return null;

    // 実行中のリクエストがあれば新規に発行せず、その結果をそのまま返す。
    // async 関数の戻り値として Promise を返すので、呼び出し側は解決後の userInfo を受け取る
    // （refreshUserInfo の戻り値で userInfo を受け取る仕組みは維持される）。
    if (inFlightFetchRef.current) {
      return inFlightFetchRef.current;
    }

    const request = fetchUserInfoOnce(currentToken);
    inFlightFetchRef.current = request;
    // 成否にかかわらず完了時に ref をクリアする。
    // 別の呼び出しが既に新しいリクエストを入れている場合は上書きしない。
    request
      .catch(() => null)
      .then(() => {
        if (inFlightFetchRef.current === request) {
          inFlightFetchRef.current = null;
        }
      });

    return request;
  };

  const fetchUserInfoOnce = async (currentToken) => {
    setUserInfoFetchFailed(false);

    for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt++) {
      try {
        const res = await fetch(ME_URL, {
          headers: { Authorization: `Bearer ${currentToken}` },
        });
        if (res.ok) {
          const data = await res.json();
          setUserInfo(data);
          return data;
        }

        console.error('Failed to fetch user info:', res.status);
        if (res.status === 401) {
          handleLogout();
          return null;
        }
        // 401 以外のクライアントエラーはリトライしても回復しない
        if (res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429) {
          setUserInfoFetchFailed(true);
          return null;
        }
      } catch (error) {
        console.error('Error fetching user info:', error);
      }

      const delay = RETRY_DELAYS_MS[attempt];
      if (delay === undefined) break;
      await new Promise((resolve) => setTimeout(resolve, delay));
    }

    setUserInfoFetchFailed(true);
    return null;
  };

  // 呼び出し側から明示的にユーザー情報を再取得するための関数。
  // state 更新は同じ呼び出しフレームには反映されないため、取得結果をそのまま返す。
  const refreshUserInfo = async () => {
    if (!token) return null;
    return fetchUserInfo(token);
  };

  return (
    <AuthContext.Provider
      value={{
        username,
        setUsername,
        password,
        setPassword,
        token,
        isAdmin,
        userInfo,
        userInfoFetchFailed,
        refreshUserInfo,
        handleLogin,
        handleLogout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};
