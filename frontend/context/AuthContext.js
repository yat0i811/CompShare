// src/context/AuthContext.js
import React, { createContext, useState, useEffect } from 'react';
import { LOGIN_URL, ME_URL, isTokenExpired } from '../utils/constants';

// AuthContext を作成
export const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [token, setToken] = useState(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [userInfo, setUserInfo] = useState(null);

  // ログアウト処理
  const handleLogout = () => {
    if (typeof window !== 'undefined') {
      localStorage.removeItem('authToken');
    }
    setToken(null);
    setIsAdmin(false);
    setUserInfo(null);
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

  // /auth/me から現在のユーザー情報を取得し、userInfo をセット
  const fetchUserInfo = async (currentToken) => {
    if (!currentToken) return;
    try {
      const res = await fetch(ME_URL, {
        headers: { Authorization: `Bearer ${currentToken}` },
      });
      if (res.ok) {
        const data = await res.json();
        setUserInfo(data);
      } else {
        console.error('Failed to fetch user info:', res.status);
        if (res.status === 401) {
          handleLogout();
        }
      }
    } catch (error) {
      console.error('Error fetching user info:', error);
    }
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
        handleLogin,
        handleLogout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};
