import { useState, useEffect } from "react";
import { LOGIN_URL, ME_URL, isTokenExpired } from '../utils/constants';

// Custom hook for authentication logic
export default function useAuth() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [userInfo, setUserInfo] = useState(null);

  const handleLogout = () => {
    if (typeof window !== 'undefined') {
      localStorage.removeItem("authToken");
      // window.location.reload(); // この行を削除またはコメントアウト
    }
    setToken(null);
    setIsAdmin(false);
    setUserInfo(null);
  };

  useEffect(() => {
    if (typeof window !== 'undefined') {
      const savedToken = localStorage.getItem("authToken");
      if (savedToken && !isTokenExpired(savedToken)) {
        setToken(savedToken);
      } else {
        handleLogout();
      }
    }
  }, []);

  useEffect(() => {
    if (token) {
      try {
        const payload = JSON.parse(atob(token.split('.')[1]));
        if (payload && payload.is_admin) {
          setIsAdmin(true);
        } else {
          setIsAdmin(false);
        }
        fetchUserInfo(token);
      } catch (error) {
        console.error('Token decode error in useAuth:', error);
        setIsAdmin(false);
      }
    } else {
      setIsAdmin(false);
    }
  }, [token]);

  useEffect(() => {
    if (!token) return;
    const interval = setInterval(() => {
      if (isTokenExpired(token)) {
        alert("セッションが切れました。再ログインしてください。");
        handleLogout();
      }
    }, 60 * 1000);
    return () => clearInterval(interval);
  }, [token]);

  const fetchUserInfo = async (currentToken) => {
    if (!currentToken) return;
    try {
      const res = await fetch(ME_URL, {
        headers: {
          'Authorization': `Bearer ${currentToken}`
        }
      });
      if (res.ok) {
        const data = await res.json();
        setUserInfo(data);
      } else {
        console.error("Failed to fetch user info:", res.status);
        if (res.status === 401) {
          handleLogout();
        }
      }
    } catch (error) {
      console.error("Error fetching user info:", error);
    }
  };

  const handleLogin = async () => {
    const formData = new FormData();
    formData.append("username", username);
    formData.append("password", password);
    try {
      console.log('Attempting login to:', LOGIN_URL);
      const res = await fetch(LOGIN_URL, { method: "POST", body: formData });
      if (res.ok) {
        const data = await res.json();
        setToken(data.token);
        if (typeof window !== 'undefined') {
          localStorage.setItem("authToken", data.token);
        }
      } else {
        const errorData = await res.json().catch(() => ({ detail: "パスワードが間違っているか、サーバーエラーが発生しました。" }));
        alert(errorData.detail || "ログインに失敗しました。");
      }
    } catch (error) {
      console.error("Login error:", error);
      alert("ログインリクエスト中にエラーが発生しました。");
    }
  };

  return {
    username, setUsername,
    password, setPassword,
    token,
    isAdmin,
    userInfo,
    handleLogin,
    handleLogout,
    fetchUserInfo
  };
} 