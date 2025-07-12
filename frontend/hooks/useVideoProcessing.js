import { useState, useEffect, useRef } from "react";
import { v4 as uuidv4 } from "uuid";
import {
    BASE_URL, 
    GET_UPLOAD_URL_ENDPOINT, 
    COMPRESS_URL_ENDPOINT,
    DOWNLOAD_URL_ENDPOINT,
    WS_URL_BASE,
    isLocalhost,
    isTokenExpired,
    CREATE_SHARE_URL,
    GET_SHARES_URL,
    PUBLIC_DOWNLOAD_URL
} from '../utils/constants';

// Custom hook for video processing logic
export default function useVideoProcessing({ token, handleLogout, userInfo }) {
  const [file, setFile] = useState(null);
  const [originalVideoUrl, setOriginalVideoUrl] = useState("");
  const [originalFileSize, setOriginalFileSize] = useState(0);
  const [compressedVideoUrl, setCompressedVideoUrl] = useState("");
  const [compressedFileName, setCompressedFileName] = useState("");
  const [compressedFileSize, setCompressedFileSize] = useState(0);
  const [progress, setProgress] = useState(0);
  const [clientId] = useState(uuidv4());
  const [crf, setCrf] = useState(28);
  const [bitrate, setBitrate] = useState(3); // ビットレート設定（Mbps）
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

  const isExternal = typeof window !== "undefined" && !isLocalhost();
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
        if (data.type === "done") {
          setCompressedVideoUrl(data.url);
          setCompressedFileName(data.filename);
          setCompressedFileSize(data.size || 0);
          setCompressedR2Key(data.r2_key || ""); // R2キーを保存
          setProgress(100);
          setIsUploading(false);
          setErrorMessage("");
        } else if (data.type === "progress") {
          setProgress(data.value);
        } else if (data.type === "warning") {
          setErrorMessage(`⚠️ ${data.detail}`);
        } else if (data.type === "error") {
          setErrorMessage(data.detail || "サーバーで圧縮エラーが発生しました。");
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
    };

    return () => {
      if (ws.current) {
        ws.current.close();
      }
    };
  }, [clientId, token]);

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

  const estimateCompressedSize = (originalSize, crfValue) => {
    const baseCrf = 18;
    const compressionRate = 0.1285;
    const factor = Math.pow(1 - compressionRate, crfValue - baseCrf);
    return originalSize * factor;
  };

  // 動画の解像度を取得してビットレートのデフォルト値を設定
  const getVideoDimensions = (file) => {
    return new Promise((resolve) => {
      const video = document.createElement('video');
      video.preload = 'metadata';
      
      video.onloadedmetadata = () => {
        const width = video.videoWidth;
        const height = video.videoHeight;
        
        // 解像度に応じたデフォルトビットレート設定
        let defaultBitrate;
        if (width >= 3840 || height >= 2160) {  // 4K
          defaultBitrate = 8;
        } else if (width >= 1920 || height >= 1080) {  // 1080p
          defaultBitrate = 3;
        } else if (width >= 1280 || height >= 720) {  // 720p
          defaultBitrate = 2;
        } else {  // 480p以下
          defaultBitrate = 1;
        }
        
        resolve({ width, height, defaultBitrate });
      };
      
      video.onerror = () => {
        // エラーの場合はデフォルト値を返す
        resolve({ width: 1920, height: 1080, defaultBitrate: 3 });
      };
      
      video.src = URL.createObjectURL(file);
    });
  };

  const handleUpload = async () => {
    if (!file || !token || isUploading) return;

    // ユーザーのアップロード容量を取得 (userInfo が利用可能であることを想定)
    // userInfo.upload_capacity_bytes が存在しない場合のデフォルト値を設定 (例: 100MB)
    const userCapacity = userInfo && userInfo.upload_capacity_bytes ? userInfo.upload_capacity_bytes : 104857600;

    if (isTokenExpired(token)) {
      alert("セッションが切れました。再ログインしてください。");
      handleLogout();
      return;
    }

    setIsUploading(true);
    setErrorMessage("");
    setProgress(0);
    resetStates(); // 共有関連の状態をリセット
    setOriginalVideoUrl(URL.createObjectURL(file));
    setOriginalFileSize(file.size);

    try {
      if (!file.type.startsWith("video/")) {
        setErrorMessage("動画ファイルのみアップロードできます（例: mp4）。サポートされている形式か確認してください。");
        setIsUploading(false);
        return;
      }

      // ユーザーごとのアップロード容量上限チェック
      if (file.size > userCapacity) {
        setErrorMessage(`ファイルサイズが大きすぎます。あなたの上限は ${Math.floor(userCapacity / (1024*1024))} MBです。`);
        setIsUploading(false);
        return;
      }
      
      // if (isExternal && file.size > MAX_FILE_SIZE) { // ユーザー別容量チェックに含めるためコメントアウト
      //   setErrorMessage("外部アクセスでは1GBを超える動画はアップロードできません。");
      //   setIsUploading(false);
      //   return;
      // }

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

      const r2UploadRes = await fetch(upload_url, { method: "PUT", body: file });
      if (!r2UploadRes.ok) {
        setErrorMessage("R2へのファイルアップロードに失敗しました。");
        setIsUploading(false);
        return;
      }
      
      const compressFormData = new FormData();
      compressFormData.append("filename", file.name);
      compressFormData.append("crf", crf);
      compressFormData.append("bitrate", bitrate);
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

      // ダウンロードURLを構築
      const downloadUrl = `${DOWNLOAD_URL_ENDPOINT}${encodeURIComponent(compressedFileName)}`;
      
      // 認証トークン付きでダウンロード
      const downloadResponse = await fetch(downloadUrl, {
        headers: { 
          Authorization: `Bearer ${token}` 
        }
      });

      if (!downloadResponse.ok) {
        if (downloadResponse.status === 404) {
          throw new Error("ファイルが見つかりません。圧縮処理が完了していない可能性があります。");
        } else if (downloadResponse.status === 401) {
          throw new Error("認証エラーです。再ログインしてください。");
        } else {
          throw new Error(`ダウンロードエラー (${downloadResponse.status})`);
        }
      }

      const blob = await downloadResponse.blob();
      
      // Create a blob URL
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = compressedFileName;
      document.body.appendChild(a);
      a.click();
      // Clean up by revoking the blob URL and removing the link
      document.body.removeChild(a);
      URL.revokeObjectURL(blobUrl);
      setIsDownloading(false);
      
    } catch (error) {
      console.error("Error downloading the video:", error);
      setErrorMessage(error.message || "動画のダウンロード中にエラーが発生しました。");
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
    setCompressedVideoUrl("");
    setCompressedFileName("");
    setCompressedFileSize(0);
    setCompressedR2Key("");
    setShareUrl("");
    setShareMessage("");
    setProgress(0);
    setErrorMessage("");
  };

  return {
    file, setFile,
    originalVideoUrl, setOriginalVideoUrl,
    originalFileSize, setOriginalFileSize,
    compressedVideoUrl, setCompressedVideoUrl,
    compressedFileName, setCompressedFileName,
    compressedFileSize, setCompressedFileSize,
    progress, setProgress,
    crf, setCrf,
    bitrate, setBitrate,
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
    estimateCompressedSize,
    getVideoDimensions,
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