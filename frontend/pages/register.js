import { useState } from "react";
import { REGISTER_URL } from "../utils/constants";

export default function Register() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [isRegistered, setIsRegistered] = useState(false);

  const handleRegister = async () => {
    const formData = new FormData();
    formData.append("username", username);
    formData.append("password", password);

    const res = await fetch(REGISTER_URL, {
      method: "POST",
      body: formData,
    });

    if (res.ok) {
      const data = await res.json();
      setMessage(data.message);
      setIsRegistered(true);
    } else {
      const err = await res.json();
      setMessage(`⚠️ ${err.detail}`);
      setIsRegistered(false);
    }
  };

  return (
    <div className="container">
      <h2>ユーザー登録</h2>
      {!isRegistered && (
        <>
          <input type="text" placeholder="ユーザー名" value={username} onChange={(e) => setUsername(e.target.value)} />
          <input type="password" placeholder="パスワード" value={password} onChange={(e) => setPassword(e.target.value)} />
          <button onClick={handleRegister}>登録</button>
        </>
      )}
      {message && <p className="registration-message">{message}</p>}
      <style jsx>{`
        .container {
          max-width: 400px;
          margin: auto;
          padding: 2rem;
          text-align: center;
        }
        input {
          width: 100%;
          padding: 0.5rem;
          margin-bottom: 1rem;
        }
        button {
          padding: 0.5rem 1rem;
          background: #0070f3;
          color: white;
          border: none;
          border-radius: 6px;
          cursor: pointer;
        }
        .registration-message {
          word-break: keep-all;
        }
      `}</style>
    </div>
  );
}
