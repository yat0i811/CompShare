import { useState, useEffect, useRef } from "react";
import { v4 as uuidv4 } from "uuid";
import {
    BASE_URL, 
    GET_UPLOAD_URL_ENDPOINT, 
    COMPRESS_URL_ENDPOINT,
    DOWNLOAD_URL_ENDPOINT,
    GET_DIRECT_DOWNLOAD_URL_ENDPOINT,
    WS_URL_BASE,
    isLocalhost,
    isTokenExpired,
    CREATE_SHARE_URL,
    GET_SHARES_URL,
    PUBLIC_DOWNLOAD_URL
} from '../utils/constants';

/**
 * R2 の署名付き URL へ PUT する。送信進捗を onProgress(percent, etaSec) で通知する。
 *
 * fetch を使わない理由: fetch は送信（アップロード）の進捗を取得できないため、
 * 1.2GB の送信中ずっと 0% のままになり「固まった」ように見える。
 *
 * 実装上の注意（いずれも守らないと 400/403 になる）:
 *  - Content-Type を setRequestHeader で明示しないこと。
 *    署名は Params={'Bucket','Key'} のみで作られており ContentType は SignedHeaders に
 *    入っていない。File を send() に渡せば file.type が自動付与され、未署名ヘッダとして
 *    無視される。明示指定すると将来 Params に ContentType を足したとき不一致で 403 になる。
 *  - Authorization ヘッダを付けないこと。署名済み URL に付けると SigV4 の二重認証で 400。
 *  - timeout は 0（無制限）のまま。大容量の送信に既定タイムアウトを掛けない。
 */
function uploadToR2(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url, true);
    xhr.timeout = 0;

    const started = Date.now();
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const percent = Math.min(Math.floor((e.loaded / e.total) * 100), 99);
      const elapsed = (Date.now() - started) / 1000;
      const etaSec = e.loaded > 0 && elapsed > 0
        ? Math.round((elapsed * (e.total - e.loaded)) / e.loaded)
        : null;
      onProgress(percent, etaSec);
    };

    // upload.onload（送信完了）と xhr.onload（応答受信）は別物。
    // 大容量では両者の間に無視できない待ちが生じるので、ここを繋がないと
    // 100% 表示のまま止まって見える。
    xhr.upload.onload = () => onProgress(100, 0);

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else if (xhr.status === 403) {
        reject(new Error("アップロードURLの有効期限が切れました。もう一度お試しください。"));
      } else {
        reject(new Error(`R2へのファイルアップロードに失敗しました（HTTP ${xhr.status}）。`));
      }
    };

    // onerror は CORS 失敗時に詳細を渡さない仕様なので、原因を断定しない文言にする
    xhr.onerror = () => reject(new Error("R2への送信に失敗しました（ネットワークまたはCORS設定を確認してください）。"));
    xhr.ontimeout = () => reject(new Error("R2への送信がタイムアウトしました。"));
    xhr.onabort = () => reject(new Error("R2への送信が中断されました。"));

    xhr.send(file);
  });
}

// Custom hook for video processing logic
export default function useVideoProcessing({ token, handleLogout, userInfo, refreshUserInfo }) {
  const [file, setFile] = useState(null);
  const [originalVideoUrl, setOriginalVideoUrl] = useState("");
  const [originalFileSize, setOriginalFileSize] = useState(0);
  const [compressedVideoUrl, setCompressedVideoUrl] = useState("");
  const [compressedFileName, setCompressedFileName] = useState("");
  const [compressedFileSize, setCompressedFileSize] = useState(0);
  const [progress, setProgress] = useState(0);
  // 処理段階。所要時間が桁違いの3段階を1本のバーに統合すると、
  // 20分間1%も動かない区間ができて「固まった」ように見えるため、段階ごとに表示する。
  // name: idle | sending | starting | queued | fetching | encoding | storing | done | error | disconnected
  const [stage, setStage] = useState({ name: "idle", percent: 0, etaSec: null, queuePosition: null });
  const [clientId] = useState(uuidv4());
  const [crf, setCrf] = useState(28);
  const [resolution, setResolution] = useState("source");
  const [customWidth, setCustomWidth] = useState("");
  const [customHeight, setCustomHeight] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [modifiedFile, setModifiedFile] = useState(null);
  const [modifiedVideoUrl, setModifiedVideoUrl] = useState("");
  const [useGPU, setUseGPU] = useState(true);

  // 共有機能の状態
  const [compressedR2Key, setCompressedR2Key] = useState("");
  const [shareUrl, setShareUrl] = useState("");
  const [shareExpiry, setShareExpiry] = useState(3); // デフォルト3日
  const [isCreatingShare, setIsCreatingShare] = useState(false);
  const [shareMessage, setShareMessage] = useState("");

  const ws = useRef(null);
  // サーバー由来の段階にいる間、一定時間メッセージが来なければ無応答とみなすための時刻。
  const lastServerMessageAt = useRef(0);
  // 直近に発行した元動画の blob URL。差し替え時とアンマウント時に必ず revoke する。
  // revoke を怠ると選択のたびにファイル1本分のリソースがタブ内に滞留する
  // （ブラウザがタブを閉じるまで元動画の実体をメモリ/ディスクに保持し続けるため）。
  const originalObjectUrlRef = useRef("");
  // 直近に発行した「圧縮結果」の blob URL（localhost 経路のみ。外部環境の done では
  // サーバー由来の http URL が入るので、その場合ここは空のままにする）。
  // 元動画側と同じく、差し替え時・resetStates 時・アンマウント時に必ず revoke する。
  // 圧縮結果は元動画と同程度のサイズがあるため、revoke を怠ると圧縮のたびに
  // タブ内へ1本分が滞留する。
  const compressedObjectUrlRef = useRef("");

  // 圧縮結果の blob URL を解放する。解放済みかどうかを ref で一元管理し、
  // 二重 revoke（無害だが状態が追えなくなる）を避ける。
  const releaseCompressedObjectUrl = () => {
    if (compressedObjectUrlRef.current) {
      URL.revokeObjectURL(compressedObjectUrlRef.current);
      compressedObjectUrlRef.current = "";
    }
  };

  // サーバー由来の段階（＝進捗がサーバーから push される段階）かどうか。
  const SERVER_STAGES = ["starting", "queued", "fetching", "encoding", "storing"];

  const isExternal = typeof window !== "undefined" && !isLocalhost();

  // ファイル選択の唯一の入り口。file / originalVideoUrl / originalFileSize の3つを
  // 常に整合させて更新する（バラバラに setFile だけ呼ぶと元動画カードのサイズ表示が
  // 古いファイルのままになる、といった不整合を防ぐ）。
  const selectFile = (nextFile) => {
    if (originalObjectUrlRef.current) URL.revokeObjectURL(originalObjectUrlRef.current);
    originalObjectUrlRef.current = "";

    if (!nextFile) {
      setFile(null);
      setOriginalVideoUrl("");
      setOriginalFileSize(0);
      return;
    }

    const url = URL.createObjectURL(nextFile);
    originalObjectUrlRef.current = url;
    setFile(nextFile);
    setOriginalVideoUrl(url);
    setOriginalFileSize(nextFile.size);
  };

  useEffect(() => {
    // アンマウント時のみ。selectFile 自体は差し替え時に前の URL を revoke しているので、
    // ここでは「最後に残っている1本」だけを片付ければよい。
    return () => {
      if (originalObjectUrlRef.current) URL.revokeObjectURL(originalObjectUrlRef.current);
      releaseCompressedObjectUrl();
    };
  }, []);
  // const MAX_FILE_SIZE = 1000 * 1024 * 1024; // この固定値は使用しないか、ユーザー容量と併用する形にする

  useEffect(() => {
    if (!token || !clientId) return;

    if (ws.current && ws.current.readyState === WebSocket.OPEN) {
      ws.current.close();
    }
    
    const socketUrl = `${WS_URL_BASE}/${clientId}?token=${token}`;
    ws.current = new WebSocket(socketUrl);

    ws.current.onopen = () => {
    };
    
    ws.current.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        lastServerMessageAt.current = Date.now();
        if (data.type === "done") {
          // サーバー由来の URL で置き換えるので、localhost 経路で発行済みの
          // blob URL が残っていればここで解放する（refのみを触るので、
          // このクロージャが古いレンダーの関数を捕まえていても正しく動く）。
          releaseCompressedObjectUrl();
          setCompressedVideoUrl(data.url);
          setCompressedFileName(data.filename);
          setCompressedFileSize(data.size || 0);
          setCompressedR2Key(data.r2_key || ""); // R2キーを保存
          // 圧縮前サイズはサーバー（R2 の head_object）の値を正とする。
          // 0 や未送信（旧バックエンド）のときは選択時に入れた file.size を保持する。
          if (typeof data.original_size === "number" && data.original_size > 0) {
            setOriginalFileSize(data.original_size);
          }
          setProgress(100);
          setStage({ name: "done", percent: 100, etaSec: null, queuePosition: null });
          setIsUploading(false);
          setErrorMessage("");
        } else if (data.type === "progress") {
          // phase 未指定は旧バックエンド互換として encoding とみなす
          const phase = data.phase || "encoding";
          setProgress(data.value);
          setStage({
            name: phase,
            percent: typeof data.value === "number" ? data.value : 0,
            etaSec: typeof data.etaSec === "number" ? data.etaSec : null,
            queuePosition: typeof data.queuePosition === "number" ? data.queuePosition : null,
          });
        } else if (data.type === "warning") {
          setErrorMessage(data.detail);
        } else if (data.type === "error") {
          setErrorMessage(data.detail || "サーバーで圧縮エラーが発生しました。");
          setStage((s) => ({ ...s, name: "error" }));
          setIsUploading(false);
        }
      } catch (err) {
        console.warn("WebSocket JSON parse error:", e.data, err);
      }
    };

    ws.current.onerror = (error) => {
      console.error("WebSocket error:", error);
    };

    ws.current.onclose = (event) => {
      // ジョブ実行中に切れたら、無反応にせず「切断」として見せる。
      // clientId はマウント中不変なので、再接続すればサーバー側の clients[client_id] が
      // 張り直され、以降の進捗は届く。取りこぼした分は percent が絶対値なので自己修復する。
      setStage((s) => (SERVER_STAGES.includes(s.name) ? { ...s, name: "disconnected" } : s));
    };

    return () => {
      if (ws.current) {
        ws.current.onclose = null; // アンマウント時の close を「切断」と誤表示しない
        ws.current.close();
      }
    };
  }, [clientId, token]);

  // 無通信ウォッチドッグ。
  // サーバー由来の段階にいる間、60秒メッセージが無ければ無応答として表示する。
  // エンコード中の percent 更新間隔は最長でも「総エンコード時間/100」
  // （30分エンコードで約18秒）なので、この閾値で誤検知しない。
  useEffect(() => {
    if (!SERVER_STAGES.includes(stage.name)) return;
    const timer = setInterval(() => {
      if (lastServerMessageAt.current && Date.now() - lastServerMessageAt.current > 60000) {
        setStage((s) => (SERVER_STAGES.includes(s.name) ? { ...s, name: "disconnected" } : s));
      }
    }, 5000);
    return () => clearInterval(timer);
  }, [stage.name]);

  const formatSize = (bytes) => {
    if (bytes === null || bytes === undefined) return '-';
    const GB = 1000 * 1024 * 1024; // 1GB = 1000MB (表示用)
    const MB = 1024 * 1024; // 1MB

    if (bytes >= GB) {
      return `${(bytes / GB).toFixed(2)} GB`;
    } else {
      return `${(bytes / MB).toFixed(2)} MB`;
    }
  };

  /**
   * 削減率を % で返す。正 = 削減、負 = 増加。
   * 判定不能（未取得 / 0 / 非数）のときは null を返す。
   * ここで null を返さないと NaN や Infinity が画面に出る。
   */
  const reductionRate = (before, after) => {
    if (typeof before !== "number" || typeof after !== "number") return null;
    if (!isFinite(before) || !isFinite(after)) return null;
    if (before <= 0) return null;
    return ((before - after) / before) * 100;
  };

  const estimateCompressedSize = (originalSize, crfValue) => {
    const baseCrf = 18;
    const compressionRate = 0.1285;
    const factor = Math.pow(1 - compressionRate, crfValue - baseCrf);
    return originalSize * factor;
  };

  // 推定サイズの表示幅。中心値（estimateCompressedSize）に掛ける下限/上限の係数。
  // 実測から求めた値ではなく、「動画の内容（動きの多さ・ノイズ量）で数割ぶれる」ことを
  // 利用者に伝えるための目安。GPU/CPU で同じ幅を使う（理由は docs\CLOSE_ISSUES.md §6-6）。
  const SIZE_ESTIMATE_LOW_FACTOR = 0.6;
  const SIZE_ESTIMATE_HIGH_FACTOR = 1.4;

  // 判定不能なときは null を返す（NaN / Infinity を画面に出さないため）。
  const estimateCompressedSizeRange = (originalSize, crfValue) => {
    const center = estimateCompressedSize(originalSize, crfValue);
    if (!Number.isFinite(center) || center <= 0) return null;
    return { min: center * SIZE_ESTIMATE_LOW_FACTOR, max: center * SIZE_ESTIMATE_HIGH_FACTOR };
  };

  // userInfo からアップロード容量を取り出す。
  // upload_capacity_bytes は 0 も取り得る有効な値なので truthy 判定ではなく型で判定する
  // （0 を「未取得」と誤判定しないため）。未取得のときのみ null を返す。
  const getUserCapacity = (info) =>
    info && typeof info.upload_capacity_bytes === "number" ? info.upload_capacity_bytes : null;

  const handleUpload = async () => {
    if (!file || isUploading) return;

    // ユーザーのアップロード容量を取得 (userInfo が未取得の場合は null のまま扱う)
    // 容量が未取得の状態で100MB等のデフォルト値にフォールバックすると、実際の上限が
    // それより大きい場合に誤って容量超過エラーを出してしまうため、ここではフォールバックしない
    let userCapacity = getUserCapacity(userInfo);

    if (isTokenExpired(token)) {
      alert("セッションが切れました。再ログインしてください。");
      handleLogout();
      return;
    }

    setIsUploading(true);
    setErrorMessage("");
    setProgress(0);
    setStage({ name: "sending", percent: 0, etaSec: null, queuePosition: null });

    try {
      if (!file.type.startsWith("video/")) {
        setErrorMessage("動画ファイルのみアップロードできます（例: mp4）。サポートされている形式か確認してください。");
        setIsUploading(false);
        return;
      }

      // 容量が未取得の場合は /auth/me を取り直してから再評価する。
      // AuthContext の fetchUserInfo は token 変更時に1回しか呼ばれないため、
      // ここで再取得しないとユーザーが何度試しても状況が変わらず、
      // 恒久的にアップロード不能になってしまう。
      if (userCapacity === null && typeof refreshUserInfo === "function") {
        const refreshedUserInfo = await refreshUserInfo();
        userCapacity = getUserCapacity(refreshedUserInfo);
      }

      // 再取得にも失敗した場合のみ、上限判定ができないため処理を中断する
      if (userCapacity === null) {
        setErrorMessage("アップロード可能容量を取得できませんでした。通信状況を確認のうえ、ページを再読み込みするか再ログインしてください。");
        setIsUploading(false);
        return;
      }

      // ユーザーごとのアップロード容量上限チェック
      if (file.size > userCapacity) {
        setErrorMessage(`ファイルサイズが大きすぎます。あなたの上限は ${Math.floor(userCapacity / (1024*1024))} MBです。`);
        setIsUploading(false);
        return;
      }

      // ローカルホスト環境ではバックエンド経由でアップロード
      if (isLocalhost()) {
        const uploadFormData = new FormData();
        uploadFormData.append("file", file);
        uploadFormData.append("filename", file.name);
        uploadFormData.append("crf", crf);
        uploadFormData.append("resolution", resolution);
        if (resolution === "custom") {
          if (!customWidth || !customHeight || parseInt(customWidth, 10) <= 0 || parseInt(customHeight, 10) <= 0) {
            setErrorMessage("カスタム解像度の幅と高さには正の数値を入力してください。");
            setIsUploading(false);
            return;
          }
          uploadFormData.append("width", customWidth);
          uploadFormData.append("height", customHeight);
        }
        uploadFormData.append("use_gpu", useGPU);
        uploadFormData.append("client_id", clientId);

        const uploadRes = await fetch(`${BASE_URL}/upload/`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
          body: uploadFormData,
        });

        if (!uploadRes.ok) {
          const errorData = await uploadRes.json().catch(() => ({ detail: "ローカルアップロードに失敗しました。" }));
          setErrorMessage(errorData.detail || "ローカルアップロードに失敗しました。");
          setIsUploading(false);
          return;
        }

        // ローカルアップロードの場合は直接ダウンロードリンクを作成
        const blob = await uploadRes.blob();
        // 前回の圧縮結果を先に解放してから発行する（元動画の selectFile と同じ手順）
        releaseCompressedObjectUrl();
        const url = URL.createObjectURL(blob);
        compressedObjectUrlRef.current = url;
        setCompressedVideoUrl(url);
        setCompressedFileName(file.name.replace(/\.[^/.]+$/, "") + "_compressed.mp4");
        setCompressedFileSize(blob.size);
        setProgress(100);
        setIsUploading(false);
        setErrorMessage("");
        return;
      }

      // 外部環境では従来のR2経由アップロード
      const getUrlRes = await fetch(`${GET_UPLOAD_URL_ENDPOINT}?filename=${encodeURIComponent(file.name)}&file_size=${file.size}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!getUrlRes.ok) {
        const errorData = await getUrlRes.json().catch(() => ({ detail: "サーバーでのアップロードURL取得に失敗しました。" }));
        setErrorMessage(errorData.detail || "アップロードURL取得に失敗しました。ファイル形式やサイズを確認してください。");
        setIsUploading(false);
        return;
      }
      const data = await getUrlRes.json();
      const { upload_url, key } = data;
      if (!upload_url || !key) {
        setErrorMessage("署名付きアップロードURLまたはキーが無効です。");
        setIsUploading(false);
        return;
      }

      // R2 への送信は fetch では進捗を取得できないため XHR を使う。
      // 1.2GB の送信中ずっと 0% のままだったのがこれで解消する。
      try {
        await uploadToR2(upload_url, file, (percent, etaSec) => {
          setProgress(percent);
          setStage({ name: "sending", percent, etaSec, queuePosition: null });
        });
      } catch (uploadError) {
        setErrorMessage(uploadError.message);
        setStage((s) => ({ ...s, name: "error" }));
        setIsUploading(false);
        return;
      }

      // 送信完了からサーバー応答までの間も無反応にならないよう段階を進める
      setStage({ name: "starting", percent: 0, etaSec: null, queuePosition: null });
      lastServerMessageAt.current = Date.now();


      const compressFormData = new FormData();
      compressFormData.append("filename", file.name);
      compressFormData.append("crf", crf);
      compressFormData.append("resolution", resolution);
      if (resolution === "custom") {
        if (!customWidth || !customHeight || parseInt(customWidth, 10) <= 0 || parseInt(customHeight, 10) <= 0) {
          setErrorMessage("カスタム解像度の幅と高さには正の数値を入力してください。");
          setIsUploading(false);
          return;
        }
        compressFormData.append("width", customWidth);
        compressFormData.append("height", customHeight);
      }
      compressFormData.append("use_gpu", useGPU);
      compressFormData.append("client_id", clientId);
      compressFormData.append("key", key);

      const compressRes = await fetch(COMPRESS_URL_ENDPOINT, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: compressFormData,
      });
      
      if (!compressRes.ok) {
        const errorData = await compressRes.json().catch(() => ({ detail: "R2経由での圧縮処理の開始に失敗しました。" }));
        setErrorMessage(errorData.detail || "圧縮処理の開始に失敗しました。サーバーログを確認してください。");
        setIsUploading(false);
        return;
      }
      
    } catch (err) {
      console.error("アップロード中にエラーが発生しました:", err);

      let errorMessage = "アップロード中にエラーが発生しました。";
      
      if (err.name === "TypeError" && err.message === "Failed to fetch") {
        errorMessage = "ネットワークエラーが発生しました。CORS設定またはサーバーの接続を確認してください。";
      } else if (err.message && err.message.includes("CORS")) {
        errorMessage = "CORSエラーが発生しました。サーバーの設定を確認してください。";
      } else if (err.message) {
        errorMessage = `エラー詳細: ${err.message}`;
      }
      
      setErrorMessage(errorMessage);
      setIsUploading(false);
    }
  };

  const downloadCompressedVideo = async () => {
    if (!compressedFileName || isDownloading) return;

    setIsDownloading(true);
    setErrorMessage("");

    try {
      // ローカルホスト環境では既にblob URLが作成されているので直接ダウンロード
      if (isLocalhost() && compressedVideoUrl && compressedVideoUrl.startsWith('blob:')) {
        const a = document.createElement("a");
        a.href = compressedVideoUrl;
        a.download = compressedFileName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setIsDownloading(false);
        return;
      }

      // 外部環境では従来のR2経由ダウンロード
      // 圧縮処理の完了を確認
      const checkResponse = await fetch(`${BASE_URL}/check-compression/${encodeURIComponent(compressedFileName)}`, {
        headers: { 
          Authorization: `Bearer ${token}` 
        }
      });

      if (!checkResponse.ok) {
        throw new Error(`圧縮状態の確認に失敗しました (${checkResponse.status})`);
      }

      const checkData = await checkResponse.json();
      
      if (checkData.status === "processing") {
        setErrorMessage("圧縮処理がまだ完了していません。しばらく待ってから再試行してください。");
        setIsDownloading(false);
        return;
      }

      // 直接ダウンロードURLを取得
      const urlResponse = await fetch(`${GET_DIRECT_DOWNLOAD_URL_ENDPOINT}${encodeURIComponent(compressedFileName)}`, {
        headers: { 
          Authorization: `Bearer ${token}` 
        }
      });

      if (!urlResponse.ok) {
        if (urlResponse.status === 404) {
          throw new Error("ファイルが見つかりません。圧縮処理が完了していない可能性があります。");
        } else if (urlResponse.status === 401) {
          throw new Error("認証エラーです。再ログインしてください。");
        } else {
          throw new Error(`ダウンロードURL取得エラー (${urlResponse.status})`);
        }
      }

      const urlData = await urlResponse.json();
      
      // 直接ダウンロードリンクを作成してクリック
      const a = document.createElement("a");
      a.href = urlData.download_url;
      a.download = compressedFileName;
      a.target = "_blank"; // 新しいタブで開く（オプション）
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      
      setIsDownloading(false);
      
    } catch (error) {
      console.error("ダウンロード中にエラーが発生しました:", error);
      setErrorMessage(error.message || "ダウンロード中にエラーが発生しました。");
      setIsDownloading(false);
    }
  };

  // 共有リンクの作成
  const createShareLink = async () => {
    if (!compressedFileName || !compressedR2Key || !token || isCreatingShare) return;
    
    if (isTokenExpired(token)) {
      alert("セッションが切れました。再ログインしてください。");
      handleLogout();
      return;
    }
    
    // ローカルホスト環境では共有機能を無効化
    if (isLocalhost()) {
      setShareMessage("ローカルホスト環境では共有機能は利用できません。");
      return;
    }
    
    setIsCreatingShare(true);
    setShareMessage("");
    setShareUrl("");
    
    try {
      const formData = new FormData();
      formData.append("compressed_filename", compressedFileName);
      formData.append("r2_key", compressedR2Key);
      formData.append("expiry_days", shareExpiry);
      
      const response = await fetch(CREATE_SHARE_URL, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ detail: "共有リンクの作成に失敗しました。" }));
        setShareMessage(errorData.detail || "共有リンクの作成に失敗しました。");
        setIsCreatingShare(false);
        return;
      }
      
      const data = await response.json();
      setShareUrl(data.share_url);
      setShareMessage(`共有リンクを作成しました（有効期限: ${shareExpiry}日）`);
      
    } catch (error) {
      console.error("Share creation error:", error);
      setShareMessage(`共有リンクの作成エラー: ${error.message}`);
    } finally {
      setIsCreatingShare(false);
    }
  };

  // 共有URLをクリップボードにコピー
  const copyShareUrl = () => {
    if (!shareUrl) return;
    
    navigator.clipboard.writeText(shareUrl).then(() => {
      setShareMessage("共有URLをクリップボードにコピーしました！");
      setTimeout(() => setShareMessage(""), 3000);
    }).catch(err => {
      console.error("Failed to copy: ", err);
      setShareMessage("クリップボードへのコピーに失敗しました。");
    });
  };

  // 状態をリセット（新しいアップロード時）
  const resetStates = () => {
    // state から参照を外す前に revoke する。ここで解放しないと、
    // ファイルを選び直すたびに前回の圧縮結果がタブ内に残り続ける。
    releaseCompressedObjectUrl();
    setCompressedVideoUrl("");
    setCompressedFileName("");
    setCompressedFileSize(0);
    setCompressedR2Key("");
    setShareUrl("");
    setShareMessage("");
    setProgress(0);
    setStage({ name: "idle", percent: 0, etaSec: null, queuePosition: null });
    setErrorMessage("");
  };

  return {
    stage,
    file, setFile,
    selectFile,
    originalVideoUrl, setOriginalVideoUrl,
    originalFileSize, setOriginalFileSize,
    compressedVideoUrl, setCompressedVideoUrl,
    compressedFileName, setCompressedFileName,
    compressedFileSize, setCompressedFileSize,
    progress, setProgress,
    crf, setCrf,
    resolution, setResolution,
    customWidth, setCustomWidth,
    customHeight, setCustomHeight,
    isUploading,
    isDownloading,
    errorMessage, setErrorMessage,
    modifiedFile, setModifiedFile,
    modifiedVideoUrl, setModifiedVideoUrl,
    useGPU, setUseGPU,
    handleUpload,
    downloadCompressedVideo,
    formatSize,
    reductionRate,
    estimateCompressedSize,
    estimateCompressedSizeRange,
    // 共有機能
    compressedR2Key,
    shareUrl,
    shareExpiry, setShareExpiry,
    isCreatingShare,
    shareMessage,
    createShareLink,
    copyShareUrl,
    resetStates,
    // MAX_FILE_SIZE,
  };
}