import { useState, useEffect, useRef } from "react";
import { v4 as uuidv4 } from "uuid";
import {
    BASE_URL, 
    GET_UPLOAD_URL_ENDPOINT, 
    COMPRESS_URL_ENDPOINT,
    DOWNLOAD_URL_ENDPOINT,
    WS_URL_BASE,
    isLocalhost,
    isTokenExpired
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
  const [resolution, setResolution] = useState("source");
  const [customWidth, setCustomWidth] = useState("");
  const [customHeight, setCustomHeight] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [modifiedFile, setModifiedFile] = useState(null);
  const [modifiedVideoUrl, setModifiedVideoUrl] = useState("");

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
          setProgress(100);
        } else if (data.type === "progress") {
          setProgress(data.value);
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
    setCompressedVideoUrl("");
    setCompressedFileSize(0);
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
      setErrorMessage("アップロード中にエラーが発生しました。ファイル形式やサイズを確認してください。");
      setIsUploading(false);
    }
  };

  const downloadCompressedVideo = () => {
    if (!compressedFileName || isDownloading) return;

    setIsDownloading(true);
    setErrorMessage("");

    // 新しいダウンロードエンドポイントを使用
    const downloadUrl = `${DOWNLOAD_URL_ENDPOINT}${encodeURIComponent(compressedFileName)}`;
    
    // 認証トークン付きでダウンロード
    fetch(downloadUrl, {
      headers: { 
        Authorization: `Bearer ${token}` 
      }
    })
    .then(response => {
      if (!response.ok) {
        if (response.status === 404) {
          throw new Error("ファイルが見つかりません。圧縮処理が完了していない可能性があります。");
        } else if (response.status === 401) {
          throw new Error("認証エラーです。再ログインしてください。");
        } else {
          throw new Error(`ダウンロードエラー (${response.status})`);
        }
      }
      return response.blob();
    })
    .then(blob => {
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
    })
    .catch(error => {
      console.error("Error downloading the video:", error);
      setErrorMessage(error.message || "動画のダウンロード中にエラーが発生しました。");
      setIsDownloading(false);
    });
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
    resolution, setResolution,
    customWidth, setCustomWidth,
    customHeight, setCustomHeight,
    isUploading,
    isDownloading,
    errorMessage, setErrorMessage,
    modifiedFile, setModifiedFile,
    modifiedVideoUrl, setModifiedVideoUrl,
    handleUpload,
    downloadCompressedVideo,
    formatSize,
    estimateCompressedSize,
    // MAX_FILE_SIZE,
  };
}